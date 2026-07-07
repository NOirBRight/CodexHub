from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from subagent_dynamic_dag import build_dynamic_dag_workflow, is_dynamic_dag_request
from subagent_protocol import ProtocolState, protocol_state_from_input_items
from subagent_scheduler import workflow_complete


MULTI_AGENT_TOOL_NAMES = {"spawn_agent", "wait_agent", "close_agent", "resume_agent", "send_input"}
REVIEW_ROLES = {"spec_reviewer", "code_quality_reviewer"}
TASK_SEQUENCE = ("task-1", "task-2")


@dataclass(frozen=True)
class SpawnRequest:
    prompt: str
    nickname: str | None
    role: str
    task_key: str
    signature: str


@dataclass
class AgentState:
    agent_id: str
    call_id: str | None
    prompt: str
    nickname: str | None
    role: str
    task_key: str
    signature: str
    epoch: int
    waited: bool = False
    closed: bool = False
    result: str = ""
    needs_input: bool = False


@dataclass
class SubagentState:
    agents: dict[str, AgentState] = field(default_factory=dict)
    protocol_state: ProtocolState | None = None
    requested_count: int | None = None
    requested_append: bool = False
    append_baseline_count: int | None = None
    bounded_request: bool = False
    workflow_intent: bool = False
    dynamic_dag_intent: bool = False
    workflow_plan_read: bool = False
    workflow_expected_artifact_text: str | None = None
    workflow_close_after_wait: bool = False
    workflow_task_count: int | None = None
    implementation_epoch_by_task: dict[str, int] = field(default_factory=dict)
    pending_fix_targets: set[str] = field(default_factory=set)
    close_waited_agents: bool = False
    next_action: str = "spawn"
    next_expected_role: str | None = None
    next_expected_task: str | None = None
    send_input_target: str | None = None
    send_input_reason: str | None = None
    lifecycle_complete: bool = False

    @property
    def spawned_agent_ids(self) -> list[str]:
        return list(self.agents.keys())

    @property
    def closed_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent and not self.protocol_state.violations:
            return self.protocol_state.closed_agent_ids
        return [agent.agent_id for agent in self.agents.values() if agent.closed]

    @property
    def open_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent and not self.protocol_state.violations:
            return self.protocol_state.open_agent_ids
        return [agent.agent_id for agent in self.agents.values() if not agent.closed]

    @property
    def wait_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent and not self.protocol_state.violations:
            ids = list(self.protocol_state.waitable_agent_ids)
            if self.next_action == "send_input":
                ids.extend(agent_id for agent_id in self.protocol_state.needs_input_agent_ids if agent_id not in ids)
            return ids
        return [agent.agent_id for agent in self.agents.values() if not agent.closed and not agent.waited]

    @property
    def close_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent and not self.protocol_state.violations:
            return self.protocol_state.closeable_agent_ids if self.close_waited_agents else []
        if not self.close_waited_agents:
            return []
        return [agent.agent_id for agent in self.agents.values() if not agent.closed and agent.waited]

    @property
    def should_allow_spawn(self) -> bool:
        return self.next_action == "spawn" and not self.lifecycle_complete

    def allows_spawn_request(self, arguments: Mapping[str, Any] | None) -> bool:
        if self.lifecycle_complete:
            return False
        request = classify_spawn_request(arguments)
        if self.bounded_request:
            if self.requested_count is None:
                return self.next_action == "spawn"
            return len(self.agents) < self.requested_count
        if self.next_action != "spawn":
            return False
        if self.next_expected_role and request.role != self.next_expected_role:
            return False
        if (
            self.next_expected_task
            and request.task_key != "general"
            and request.task_key != self.next_expected_task
        ):
            return False
        if self.requested_append:
            return True
        return _allows_spawn_request(self.agents.values(), request, self.implementation_epoch_by_task)


@dataclass(frozen=True)
class _Event:
    kind: str
    call_id: str | None = None
    agent_id: str | None = None
    arguments: Mapping[str, Any] | None = None
    result: str = ""
    results: Mapping[str, str] | None = None
    targets: tuple[str, ...] = ()
    target: str | None = None


def build_subagent_state(input_items: Any) -> SubagentState:
    events = _events_from_items(input_items)
    protocol_state = protocol_state_from_input_items(input_items)
    dynamic_dag_intent = is_dynamic_dag_request(input_items)
    workflow_intent = _has_workflow_intent(input_items)
    workflow_intent = workflow_intent or dynamic_dag_intent
    requested_count = None if workflow_intent else _requested_spawn_count(input_items)
    requested_append = _has_append_intent(input_items)
    append_baseline_count = _append_baseline_agent_count(input_items) if requested_append else None
    agents, epochs, pending_fix_targets = _apply_events(events)
    dynamic_dag_complete = False
    if dynamic_dag_intent:
        dynamic_dag_workflow = build_dynamic_dag_workflow(input_items, protocol_state)
        dynamic_dag_complete = workflow_complete(dynamic_dag_workflow, protocol_state)
    state = SubagentState(
        agents=agents,
        protocol_state=protocol_state,
        requested_count=requested_count,
        requested_append=requested_append,
        append_baseline_count=append_baseline_count,
        bounded_request=requested_count is not None,
        workflow_intent=workflow_intent,
        dynamic_dag_intent=dynamic_dag_intent,
        workflow_plan_read=(
            _has_workflow_plan_read_context(input_items) if workflow_intent and not dynamic_dag_intent else False
        ),
        workflow_expected_artifact_text=_workflow_expected_artifact_text(input_items) if workflow_intent else None,
        workflow_close_after_wait=_has_workflow_close_after_wait_intent(input_items),
        workflow_task_count=_workflow_task_count(input_items) if workflow_intent else None,
        implementation_epoch_by_task=epochs,
        pending_fix_targets=pending_fix_targets,
        lifecycle_complete=dynamic_dag_complete,
    )
    _compute_next_action(state)
    return state


def classify_spawn_request(arguments: Mapping[str, Any] | None) -> SpawnRequest:
    args = arguments if isinstance(arguments, Mapping) else {}
    prompt = _string_value(args.get("message") or args.get("prompt") or args.get("input"))
    nickname = _string_value(args.get("nickname"))
    text = " ".join(part for part in (nickname, prompt) if part).strip()
    role = _infer_role(text)
    task_key = _infer_task_key(text, role)
    if role != "generic" and task_key != "general":
        signature = f"{role}:{task_key}"
    elif nickname:
        signature = f"{role}:{_normalize_signature(nickname)}"
    else:
        signature = f"{role}:{_normalize_signature(prompt)}"
    return SpawnRequest(prompt=prompt, nickname=nickname or None, role=role, task_key=task_key, signature=signature)


def state_guidance_message(state: SubagentState) -> dict[str, str] | None:
    lines = ["Codex native multi_agent_v1 current state"]
    if state.lifecycle_complete:
        lines.append("status: lifecycle_complete")
        if state.closed_agent_ids:
            lines.append(f"closed_agent_ids: {', '.join(state.closed_agent_ids)}")
        lines.append(
            "completed_tool_aliases: multi_agent_v1__spawn_agent, multi_agent_v1__wait_agent, multi_agent_v1__close_agent"
        )
        lines.append(
            "visible_response_required: emit the final report as ordinary assistant message content, not only reasoning, analysis, hidden notes, or tool arguments. If you emit only reasoning, the user receives an empty final answer."
        )
        lines.append(
            "empty_final_forbidden: the next assistant response must contain visible text; stopping with zero visible output is a task failure."
        )
        lines.append(
            "final_format_required: use exactly the final response format requested by the user; the first visible output token must be the first token of that requested final report, with no prose preface."
        )
        lines.append(
            "required_next_action: write the final concise report now from the observed agent ids, wait sentinels, and close state in the current-turn transcript. The requested subagent lifecycle already completed via real Codex native tool executions; hidden tools after close indicate lifecycle complete, not unavailable."
        )
        return _developer_text_message("\n".join(lines))

    if state.workflow_intent and not state.dynamic_dag_intent and not state.workflow_plan_read and not state.agents:
        lines.append("status: workflow_plan_read_required")
        lines.append(
            "required_next_action: call mcp__node_repl__js now to read the subagent-driven-development skill and diagnostic plan before spawning any child agent. Use await import(\"node:fs\") inside node_repl; do not use require() or a static import statement."
        )
        lines.append(
            "example_node_repl_code: const fs = await import(\"node:fs\"); const text = fs.readFileSync(\"PLAN_PATH\", \"utf8\"); nodeRepl.write(text);"
        )
        lines.append("do_not_spawn_until: the current-turn node_repl result contains the diagnostic plan text.")
        return _developer_text_message("\n".join(lines))

    if state.next_action == "spawn" and state.bounded_request and state.requested_count is not None and state.agents:
        remaining_count = max(0, state.requested_count - len(state.agents))
        lines.append("status: spawn_more_required")
        lines.append(f"requested_spawn_count: {state.requested_count}")
        lines.append(f"completed_spawn_count: {len(state.agents)}")
        lines.append(f"remaining_spawn_count: {remaining_count}")
        lines.append(f"already_spawned_agent_ids: {', '.join(state.spawned_agent_ids)}")
        lines.append(
            "required_next_action: call multi_agent_v1__spawn_agent for the next not-yet-created child agent before waiting or closing any child agents."
        )
        return _developer_text_message("\n".join(lines))

    if state.next_action == "spawn" and state.next_expected_role:
        lines.append("status: next_subagent_spawn_required")
        lines.append(f"next_expected_role: {state.next_expected_role}")
        if state.next_expected_task:
            lines.append(f"next_expected_task: {state.next_expected_task}")
        if state.workflow_plan_read:
            lines.append("workflow_plan_read_status: completed_via_real_node_repl_current_turn")
            lines.append(
                "current_transcript_is_authoritative: the subagent-driven-development skill and diagnostic plan have already been read by a real tool result in this conversation."
            )
        if state.workflow_expected_artifact_text:
            lines.append("workflow_expected_artifact_exact_text:")
            lines.extend(state.workflow_expected_artifact_text.splitlines())
            lines.append(
                "workflow_artifact_instruction: include this exact expanded text in implementer and reviewer prompts when they create or verify the diagnostic artifact. Preserve LF newline separators between records; a concatenated one-line artifact is a failure. Tell the spec reviewer to compare raw text or bytes and fail on missing, collapsed, or replaced newline separators. Coordinator input names such as MODEL_UNDER_TEST and ENDPOINT_UNDER_TEST are variables, not artifact field names, unless the plan explicitly says otherwise."
            )
        lines.append(
            "required_next_action: call multi_agent_v1__spawn_agent for this distinct role/task now. Do not answer with prose, do not produce empty output, and do not repeat an already spawned role/task in the same implementation epoch."
        )
        return _developer_text_message("\n".join(lines))

    if state.next_action == "wait" and state.wait_agent_ids:
        lines.append("status: spawned_child_wait_required")
        lines.append(f"open_agent_ids_requiring_wait: {', '.join(state.wait_agent_ids)}")
        lines.append(
            "required_next_action: call multi_agent_v1__wait_agent with targets set to these agent_id values and timeout_ms=60000."
        )
        return _developer_text_message("\n".join(lines))

    if state.next_action == "close" and state.close_agent_ids:
        lines.append("status: wait_completed_close_required")
        lines.append(f"open_agent_ids_requiring_close: {', '.join(state.close_agent_ids)}")
        lines.append(
            "required_next_action: call multi_agent_v1__close_agent with target set to one listed agent_id. "
            "Do not write the final report until every listed agent_id has been closed."
        )
        return _developer_text_message("\n".join(lines))

    if state.next_action == "send_input" and state.send_input_target:
        if state.send_input_reason == "implementer_incomplete":
            lines.append("status: implementer_incomplete_fix_required")
        elif state.send_input_reason == "child_empty_output":
            lines.append("status: child_empty_output_fix_required")
        else:
            lines.append("status: reviewer_issue_fix_required")
        lines.append(f"send_input_target: {state.send_input_target}")
        if state.send_input_reason == "implementer_incomplete":
            lines.append(
                "required_next_action: call multi_agent_v1__send_input or multi_agent_v1__resume_agent for this existing implementer with precise fix instructions; do not spawn a reviewer until the implementer returns explicit DONE."
            )
        elif state.send_input_reason == "child_empty_output":
            agent = state.agents.get(state.send_input_target)
            if agent is not None and agent.prompt:
                lines.append(f"original_child_prompt: {agent.prompt}")
            lines.append(
                "required_next_action: call multi_agent_v1__send_input for this existing child because its completed wait result had empty visible output. Ask it to return exactly the output requested in its original prompt, with no prose or markdown, then wait for the same child again before closing."
            )
        else:
            lines.append(
                "required_next_action: call multi_agent_v1__send_input or multi_agent_v1__resume_agent for this existing implementer; do not spawn another reviewer before the fix completes."
            )
        return _developer_text_message("\n".join(lines))

    return None


def _developer_text_message(content: str) -> dict[str, str]:
    return {"type": "message", "role": "developer", "content": content}


def _apply_events(events: Iterable[_Event]) -> tuple[dict[str, AgentState], dict[str, int], set[str]]:
    agents: dict[str, AgentState] = {}
    epochs: dict[str, int] = {}
    pending_fix_targets: set[str] = set()

    for event in events:
        if event.kind == "spawn" and event.agent_id:
            request = classify_spawn_request(event.arguments)
            if request.role == "implementer":
                epochs[request.task_key] = epochs.get(request.task_key, 0) + 1
            epoch = epochs.get(request.task_key, 0)
            agents[event.agent_id] = AgentState(
                agent_id=event.agent_id,
                call_id=event.call_id,
                prompt=request.prompt,
                nickname=request.nickname,
                role=request.role,
                task_key=request.task_key,
                signature=request.signature,
                epoch=epoch,
            )
            continue

        if event.kind == "wait":
            for agent_id in event.targets or ((event.agent_id,) if event.agent_id else ()):
                agent = agents.get(agent_id)
                if agent is None:
                    continue
                agent_result = event.results.get(agent_id, event.result) if event.results else event.result
                if not agent_result.strip():
                    agent.waited = False
                    agent.result = ""
                    agent.needs_input = True
                    continue
                if agent_id in pending_fix_targets and agent.role == "implementer":
                    epochs[agent.task_key] = max(epochs.get(agent.task_key, agent.epoch), agent.epoch) + 1
                    agent.epoch = epochs[agent.task_key]
                    pending_fix_targets.discard(agent_id)
                agent.waited = True
                agent.result = agent_result
                agent.needs_input = False
            continue

        if event.kind == "close":
            targets = event.targets or ((event.target,) if event.target else ())
            for agent_id in targets:
                agent = agents.get(agent_id)
                if agent is not None:
                    agent.closed = True
            continue

        if event.kind in {"send_input", "resume"} and event.target:
            pending_fix_targets.add(event.target)
            agent = agents.get(event.target)
            if agent is not None:
                agent.waited = False
                agent.result = ""
                agent.needs_input = False
                if event.kind == "resume":
                    agent.closed = False

    return agents, epochs, pending_fix_targets


def _compute_next_action(state: SubagentState) -> None:
    if state.bounded_request:
        state.close_waited_agents = True
        if state.requested_count is not None and len(state.agents) < state.requested_count:
            state.next_action = "spawn"
            return
        empty_target = _open_agent_needing_input(state.agents.values())
        if empty_target is not None:
            state.next_action = "send_input"
            state.send_input_target = empty_target.agent_id
            state.send_input_reason = "child_empty_output"
            return
        if state.wait_agent_ids:
            state.next_action = "wait"
            return
        if state.close_agent_ids:
            state.next_action = "close"
            return
        if state.requested_count is not None and state.closed_agent_ids and len(state.closed_agent_ids) >= state.requested_count:
            state.lifecycle_complete = True
            state.next_action = "final"
            return
        state.next_action = "spawn" if not state.agents else "final"
        return

    if state.dynamic_dag_intent:
        state.close_waited_agents = True
        if state.lifecycle_complete:
            state.next_action = "final"
            return
        empty_target = _open_agent_needing_input(state.agents.values())
        if empty_target is not None:
            state.next_action = "send_input"
            state.send_input_target = empty_target.agent_id
            state.send_input_reason = "child_empty_output"
            return
        if state.wait_agent_ids:
            state.next_action = "wait"
            return
        if state.close_agent_ids:
            state.next_action = "close"
            return
        state.next_action = "spawn"
        state.next_expected_role = None
        state.next_expected_task = None
        return

    if not state.workflow_intent and not state.agents:
        state.next_action = "spawn" if state.requested_append else "idle"
        return

    if state.agents and not _has_workflow_agents(state.agents.values()):
        state.close_waited_agents = True
        if (
            state.requested_append
            and state.append_baseline_count is not None
            and len(state.agents) <= state.append_baseline_count
        ):
            state.next_action = "spawn"
            return
        if state.wait_agent_ids:
            state.next_action = "wait"
            return
        if state.close_agent_ids:
            state.next_action = "close"
            return
        state.next_action = "spawn"
        return

    # Compatibility path for existing Level 2 workflow gates.
    # New workflow behavior should be modeled in subagent_scheduler.py first,
    # then wired here after scheduler tests cover it.
    if state.workflow_intent:
        empty_target = _open_agent_needing_input(state.agents.values())
        if empty_target is not None:
            state.next_action = "send_input"
            state.send_input_target = empty_target.agent_id
            state.send_input_reason = "child_empty_output"
            return

    if state.workflow_intent and state.wait_agent_ids:
        state.next_action = "wait"
        return

    if state.workflow_close_after_wait and _has_waited_open_workflow_agent(state.agents.values()):
        state.close_waited_agents = True
        state.next_action = "close"
        return

    pending_target = _pending_fix_target(state)
    if pending_target:
        state.next_action = "wait"
        state.send_input_target = pending_target
        return

    task = _latest_current_task(state.agents.values())
    task_in_scope = state.workflow_task_count is None or _task_index(task) <= state.workflow_task_count
    implementer = _latest_implementer(state.agents.values(), task) if task_in_scope else None
    if task_in_scope:
        if implementer is None:
            state.next_action = "spawn"
            state.next_expected_role = "implementer"
            state.next_expected_task = task
            return
        if not implementer.waited:
            state.next_action = "wait"
            return
        if not _implementer_succeeded(implementer.result):
            if implementer.closed:
                state.next_action = "spawn"
                state.next_expected_role = "implementer"
                state.next_expected_task = task
                return
            state.next_action = "send_input"
            state.send_input_target = implementer.agent_id
            state.send_input_reason = "implementer_incomplete"
            return

    if not task_in_scope:
        state.lifecycle_complete = True
        state.next_action = "final"
        return

    failed_review = _latest_failed_review(state.agents.values())
    if failed_review is not None:
        failed_review_implementer = _latest_implementer(state.agents.values(), failed_review.task_key)
        if failed_review_implementer is not None:
            if failed_review_implementer.closed:
                state.next_action = "spawn"
                state.next_expected_role = "implementer"
                state.next_expected_task = failed_review.task_key
                return
            state.next_action = "send_input"
            state.send_input_target = failed_review_implementer.agent_id
            state.send_input_reason = "reviewer_issue"
            return

    epoch = _current_epoch(state.agents.values(), task)
    open_spec = _open_unwaited_review(state.agents.values(), task, "spec_reviewer", epoch)
    if open_spec is not None:
        state.next_action = "wait"
        return
    if not _review_passed(state.agents.values(), task, "spec_reviewer", epoch):
        state.next_action = "spawn"
        state.next_expected_role = "spec_reviewer"
        state.next_expected_task = task
        return

    open_quality = _open_unwaited_review(state.agents.values(), task, "code_quality_reviewer", epoch)
    if open_quality is not None:
        state.next_action = "wait"
        return
    if not _review_passed(state.agents.values(), task, "code_quality_reviewer", epoch):
        state.next_action = "spawn"
        state.next_expected_role = "code_quality_reviewer"
        state.next_expected_task = task
        return

    next_task = _next_task_key(task)
    if state.workflow_task_count is not None and _task_index(next_task) > state.workflow_task_count:
        state.lifecycle_complete = True
        state.next_action = "final"
        return

    state.next_action = "spawn"
    state.next_expected_role = "implementer"
    state.next_expected_task = next_task


def _allows_spawn_request(
    agents: Iterable[AgentState],
    request: SpawnRequest,
    epochs: Mapping[str, int],
) -> bool:
    epoch = epochs.get(request.task_key, 0)
    for agent in agents:
        if (
            agent.role == request.role
            and agent.task_key == request.task_key
            and agent.signature == request.signature
            and agent.epoch == epoch
            and not agent.closed
        ):
            if agent.waited and _contains_issue(agent.result):
                continue
            return False
    return True


def _has_workflow_agents(agents: Iterable[AgentState]) -> bool:
    return any(agent.role != "generic" for agent in agents)


def _has_waited_open_workflow_agent(agents: Iterable[AgentState]) -> bool:
    return any(agent.role != "generic" and agent.waited and not agent.closed for agent in agents)


def _pending_fix_target(state: SubagentState) -> str | None:
    for target in state.pending_fix_targets:
        agent = state.agents.get(target)
        if agent is not None and not agent.closed:
            return target
    return None


def _open_agent_needing_input(agents: Iterable[AgentState]) -> AgentState | None:
    return next((agent for agent in agents if agent.needs_input and not agent.closed), None)


def _latest_current_task(agents: Iterable[AgentState]) -> str:
    agent_list = list(agents)
    tasks = _known_tasks(agent_list)
    for task in tasks:
        task_agents = [agent for agent in agent_list if agent.task_key == task]
        quality_passed = any(
            agent.role == "code_quality_reviewer" and agent.waited and _contains_pass(agent.result)
            for agent in task_agents
        )
        if not quality_passed:
            return task
    return _next_task_key(tasks[-1] if tasks else "task-1")


def _known_tasks(agents: list[AgentState]) -> list[str]:
    found: list[str] = []
    for agent in agents:
        if agent.task_key.startswith("task-") and agent.task_key not in found:
            found.append(agent.task_key)
    if not found:
        return ["task-1"]
    return sorted(found, key=_task_sort_key)


def _next_task_key(task: str) -> str:
    match = re.fullmatch(r"task-(\d+)", task)
    if not match:
        return "task-1"
    return f"task-{int(match.group(1)) + 1}"


def _task_sort_key(task: str) -> tuple[int, str]:
    match = re.fullmatch(r"task-(\d+)", task)
    return (int(match.group(1)) if match else 9999, task)


def _task_index(task: str) -> int:
    match = re.fullmatch(r"task-(\d+)", task)
    return int(match.group(1)) if match else 9999


def _current_epoch(agents: Iterable[AgentState], task: str) -> int:
    epochs = [agent.epoch for agent in agents if agent.task_key == task and agent.role == "implementer"]
    return max(epochs) if epochs else 0


def _latest_implementer(agents: Iterable[AgentState], task: str) -> AgentState | None:
    candidates = [agent for agent in agents if agent.task_key == task and agent.role == "implementer"]
    return max(candidates, key=lambda agent: agent.epoch, default=None)


def _review_agents(agents: Iterable[AgentState], task: str, role: str, epoch: int) -> list[AgentState]:
    return [agent for agent in agents if agent.task_key == task and agent.role == role and agent.epoch == epoch]


def _review_passed(agents: Iterable[AgentState], task: str, role: str, epoch: int) -> bool:
    return any(agent.waited and _contains_pass(agent.result) for agent in _review_agents(agents, task, role, epoch))


def _open_unwaited_review(agents: Iterable[AgentState], task: str, role: str, epoch: int) -> AgentState | None:
    return next((agent for agent in _review_agents(agents, task, role, epoch) if not agent.waited), None)


def _latest_failed_review(agents: Iterable[AgentState]) -> AgentState | None:
    waited_reviews = [agent for agent in agents if agent.role in REVIEW_ROLES and agent.waited]
    for agent in reversed(waited_reviews):
        if _contains_issue(agent.result):
            implementer = _latest_implementer(waited_reviews + list(agents), agent.task_key)
            if implementer is not None and implementer.epoch == agent.epoch:
                return agent
    return None


def _implementer_succeeded(text: str) -> bool:
    if _implementer_failed_or_incomplete(text):
        return False

    lowered = text.lower()
    normalized = re.sub(r"[*_`#>~-]+", " ", lowered)
    normalized = re.sub(r"\s+", " ", normalized)
    return any(
        (
            re.search(r"\b(?:status|result|verdict)\s*:\s*done\b", normalized),
            re.search(r"(?:^|\b)done(?:\b|$)", normalized),
            re.search(r"\bimplementer\s*=\s*done\b", normalized),
            re.search(r"\bcontent_matches_spec\s*:\s*(?:yes|true)\b", normalized),
        )
    )


def _implementer_failed_or_incomplete(text: str) -> bool:
    lowered = text.lower()
    normalized = re.sub(r"[*_`#>~-]+", " ", lowered)
    normalized = re.sub(r"\s+", " ", normalized)
    failure_patterns = (
        r"\b(?:status|result|verdict)\s*:\s*(?:blocked|fail(?:ed)?|error|incomplete)\b",
        r"\b(?:blocked|needs_context|needs context|need more context)\b",
        r"\b(?:didn'?t|did not|could not|couldn'?t|cannot|can'?t)\s+(?:resolve|find|access|write|create|read)\b",
        r"\bpath\b.{0,80}\b(?:didn'?t|did not|could not|cannot|not found|missing)\b",
        r"\bnot\s+found\b",
        r"\bfailed\s+to\b",
        r"\bunable\s+to\b",
        r"\bincomplete\b",
        r"\bnot\s+done\b",
        r"\bcontent_matches_spec\s*:\s*(?:no|false)\b",
    )
    return any(re.search(pattern, normalized) for pattern in failure_patterns)


def _contains_issue(text: str) -> bool:
    explicit_status = _explicit_review_status(text)
    if explicit_status == "pass":
        return False
    if explicit_status == "fail":
        return True

    lowered = text.lower()
    if any(token in lowered for token in ("missing", "not compliant", "fail", "failed")):
        return True
    if re.search(r"\bissues?\b", lowered):
        if re.search(r"\b(no|without|zero|0)\s+(?:[a-z]+\s+){0,3}issues?\b", lowered):
            return False
        if re.search(r"\bissues?\s*:\s*(none|no|n/a|nothing)\b", lowered):
            return False
        return True
    if "问题" in text:
        return not any(token in text for token in ("没有问题", "无问题", "未发现问题"))
    if "缺失" in text:
        return not any(token in text for token in ("没有缺失", "无缺失", "未发现缺失"))
    return False


def _contains_pass(text: str) -> bool:
    explicit_status = _explicit_review_status(text)
    if explicit_status == "pass":
        return True
    if explicit_status == "fail":
        return False

    lowered = text.lower()
    if "not compliant" in lowered:
        return False
    return any(token in lowered for token in ("pass", "approved", "compliant", "✅", "通过", "批准"))


def _explicit_review_status(text: str) -> str | None:
    prefix = "\n".join(line.strip() for line in text.splitlines()[:40] if line.strip())
    normalized = prefix.lower()
    normalized = re.sub(r"[*_`#>~-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    match = re.search(r"\b(?:status|verdict|result)\s*:\s*(pass|fail|approved|failed)\b", normalized)
    if not match:
        return None
    value = match.group(1)
    return "pass" if value == "approved" else "fail" if value == "failed" else value


def _events_from_items(input_items: Any) -> list[_Event]:
    if not isinstance(input_items, list):
        return []

    calls_by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    text_calls_by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    events: list[_Event] = []

    for item in input_items:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            tool_name = _multi_agent_tool_name(item)
            call_id = _string_value(item.get("call_id")) or None
            if tool_name and call_id:
                calls_by_id[call_id] = (tool_name, _json_object(item.get("arguments")) or {})
            continue
        if item_type == "function_call_output":
            call_id = _string_value(item.get("call_id")) or None
            if not call_id or call_id not in calls_by_id:
                continue
            tool_name, arguments = calls_by_id[call_id]
            event = _event_from_tool_output(call_id, tool_name, arguments, item.get("output"))
            if event is not None:
                events.append(event)
            continue
        if item_type == "message":
            text = _joined_text(item.get("content"))
            call_entry = _text_call_entry(text)
            if call_entry is not None:
                call_id, tool_name, arguments = call_entry
                if call_id:
                    text_calls_by_id[call_id] = (tool_name, arguments)
                continue
            result_entry = _text_result_event(text, text_calls_by_id)
            if result_entry is not None:
                events.append(result_entry)

    return events


def _event_from_tool_output(
    call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    output_value: Any,
) -> _Event | None:
    output_obj = _json_object(output_value) or {}
    result = _joined_text(output_obj if output_obj else output_value)
    if tool_name == "spawn_agent":
        agent_id = _string_value(output_obj.get("agent_id")) or _string_value(output_obj.get("id"))
        return _Event("spawn", call_id=call_id, agent_id=agent_id or None, arguments=arguments, result=result)
    if tool_name == "wait_agent":
        targets = _agent_targets(arguments.get("targets"))
        completed_results = _completed_agent_results(output_obj.get("status"))
        completed = tuple(completed_results)
        if not completed:
            return None
        targets = tuple(agent_id for agent_id in targets if agent_id in completed) or completed
        return _Event(
            "wait",
            call_id=call_id,
            arguments=arguments,
            result=result,
            results=completed_results,
            targets=targets,
        )
    if tool_name == "close_agent":
        target = _string_value(arguments.get("target"))
        return _Event("close", call_id=call_id, arguments=arguments, target=target or None, targets=((target,) if target else ()))
    if tool_name in {"send_input", "resume_agent"}:
        target = _string_value(arguments.get("target") or arguments.get("agent_id") or arguments.get("id"))
        return _Event("send_input" if tool_name == "send_input" else "resume", call_id=call_id, arguments=arguments, target=target or None)
    return None


def _multi_agent_tool_name(item: Mapping[str, Any]) -> str | None:
    name = item.get("name")
    namespace = item.get("namespace")
    if namespace == "multi_agent_v1" and isinstance(name, str) and name in MULTI_AGENT_TOOL_NAMES:
        return name
    if not isinstance(name, str):
        return None
    for prefix in ("multi_agent_v1__", "mcp__multi_agent_v1__"):
        if name.startswith(prefix):
            candidate = name[len(prefix) :]
            return candidate if candidate in MULTI_AGENT_TOOL_NAMES else None
    if name.startswith("multi_agent_v1."):
        candidate = name.split(".", 1)[1]
        return candidate if candidate in MULTI_AGENT_TOOL_NAMES else None
    return name if name in MULTI_AGENT_TOOL_NAMES else None


def _text_call_entry(text: str) -> tuple[str | None, str, Mapping[str, Any]] | None:
    prefix = "Previous real Codex native multi_agent_v1."
    first_line = _first_nonempty_line(text)
    if not first_line or not first_line.startswith(prefix) or not first_line.endswith(" call transcript"):
        return None
    tool_name = first_line[len(prefix) : -len(" call transcript")]
    if tool_name not in MULTI_AGENT_TOOL_NAMES:
        return None
    return (_line_value(text, "call_id:"), tool_name, _arguments_block(text) or {})


def _text_result_event(text: str, text_calls_by_id: Mapping[str, tuple[str, Mapping[str, Any]]]) -> _Event | None:
    prefix = "Codex native multi_agent_v1."
    first_line = _first_nonempty_line(text)
    if not first_line or not first_line.startswith(prefix) or not first_line.endswith(" result"):
        return None
    tool_name = first_line[len(prefix) : -len(" result")]
    if tool_name not in MULTI_AGENT_TOOL_NAMES:
        return None
    call_id = _line_value(text, "call_id:")
    arguments = text_calls_by_id.get(call_id or "", (tool_name, {}))[1]
    if tool_name == "spawn_agent":
        agent_id = _line_value(text, "agent_id:")
        return _Event("spawn", call_id=call_id, agent_id=agent_id, arguments=arguments, result=text)
    if tool_name == "wait_agent":
        targets = tuple(_split_agent_ids(_line_value(text, "completed_agent_ids:")))
        return _Event("wait", call_id=call_id, arguments=arguments, result=text, targets=targets)
    if tool_name == "close_agent":
        target = _line_value(text, "closed_agent_id:") or _line_value(text, "target_agent_id:")
        return _Event("close", call_id=call_id, arguments=arguments, target=target, targets=((target,) if target else ()))
    return None


def _requested_spawn_count(value: Any) -> int | None:
    text = (_active_user_request_text(value) if isinstance(value, list) else _request_text(value)).lower()
    if _looks_like_worker_subagent_prompt(text):
        return None
    if not any(token in text for token in ("spawn", "subagent", "sub-agent", "agent", "子代理", "multi_agent")):
        return None

    for pattern in (
        r"(?:spawn|spawns|create|start|launch|创建|启动|派发|调用|开|生成)[^\S\r\n]*(?<![\d第])(\d{1,2})[^\S\r\n]*(?:个|名|位)?[^\S\r\n]*(?:sub-?agents?|agents?|子代理)",
        r"(?<![\d第])(\d{1,2})[^\S\r\n]*(?:个|名|位)?[^\S\r\n]*(?:sub-?agents?|agents?|子代理)",
    ):
        match = re.search(pattern, text)
        if match:
            count = int(match.group(1))
            return count if 0 < count <= 20 else None

    word_numbers = {
        "one": 1,
        "single": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "一个": 1,
        "一": 1,
        "两个": 2,
        "两": 2,
        "二个": 2,
        "二": 2,
        "三个": 3,
        "三": 3,
        "四个": 4,
        "四": 4,
        "五个": 5,
        "五": 5,
        "六个": 6,
        "六": 6,
        "七个": 7,
        "七": 7,
        "八个": 8,
        "八": 8,
        "九个": 9,
        "九": 9,
        "十个": 10,
        "十": 10,
    }
    pattern = "|".join(sorted((re.escape(key) for key in word_numbers), key=len, reverse=True))
    match = re.search(rf"\b({pattern})\b\s*(?:sub-?agents?|agents?)", text)
    if match:
        return word_numbers[match.group(1)]
    match = re.search(rf"\b({pattern})[\s-]*(?:sub-?agent|agent|child)\b", text)
    if match:
        return word_numbers[match.group(1)]
    match = re.search(rf"(?<!第)({pattern})\s*(?:个|名|位)?\s*子代理", text)
    if match:
        return word_numbers[match.group(1)]
    if re.search(r"\bspawn\s+child\s+a\b", text) and re.search(r"\bspawn\s+child\s+b\b", text):
        return 2
    if any(token in text for token in ("exactly one", "only one", "执行一次真实", "只执行一次", "一次真实", "一个子代理")):
        return 1
    return None


def _has_append_intent(value: Any) -> bool:
    text = _request_text(value).lower()
    return any(token in text for token in ("another", "second", "next child", "再", "另一个", "第二个", "追加"))


def _has_workflow_intent(value: Any) -> bool:
    text = _active_user_request_text(value).lower().replace("-", "_")
    if _looks_like_worker_subagent_prompt(text):
        return False
    if (
        "$superpowers:subagent_driven_development" in text
        or re.search(r"\buse(?:\s+the\s+real)?\s+subagent[_\s]+driven[_\s]+development\b", text)
        or re.search(r"\bexecute\s+(?:the\s+)?(?:real\s+)?subagent[_\s]+driven[_\s]+development\b", text)
    ):
        return True
    has_implementer = "implementer" in text or re.search(r"\bimplement\b", text) is not None
    has_spec_reviewer = "spec_reviewer" in text or "spec reviewer" in text or "spec compliance" in text
    has_quality_reviewer = (
        "code_quality" in text
        or "code quality" in text
        or "quality_reviewer" in text
        or "quality reviewer" in text
    )
    has_coordinator_context = any(
        token in text
        for token in (
            "coordinator",
            "execution constraints",
            "final coordinator response",
            "spawn exactly one implementer",
            "spawn an implementer",
        )
    )
    return bool(has_coordinator_context and has_implementer and has_spec_reviewer and has_quality_reviewer)


def _has_workflow_plan_read_context(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    node_repl_call_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        call_id = _string_value(item.get("call_id"))
        if item_type == "function_call" and call_id:
            name = _string_value(item.get("name"))
            namespace = _string_value(item.get("namespace"))
            if name in {"mcp__node_repl__js", "node_repl__js"} or (
                namespace in {"mcp__node_repl", "node_repl"} and name == "js"
            ):
                node_repl_call_ids.add(call_id)
            continue
        if item_type == "function_call_output" and call_id in node_repl_call_ids:
            if _looks_like_workflow_plan_text(_joined_text(item.get("output"))):
                return True
            continue
        if item_type == "message":
            text = _joined_text(item.get("content"))
            if "codex native mcp__node_repl.js result" in text.lower() and _looks_like_workflow_plan_text(text):
                return True
    return False


def _looks_like_workflow_plan_text(text: str) -> bool:
    lowered = text.lower()
    if "# short subagent development e2e plan" in lowered:
        return True
    return (
        "output_path" in lowered
        and "sentinel" in lowered
        and "implementer" in lowered
        and ("spec reviewer" in lowered or "spec compliance" in lowered)
        and ("quality reviewer" in lowered or "code quality" in lowered)
    )


def _workflow_expected_artifact_text(value: Any) -> str | None:
    combined_text = _joined_text(value).lower()
    if not (
        "sentinel=<sentinel>" in combined_text
        and "model=<model_under_test>" in combined_text
        and "endpoint=<endpoint_under_test>" in combined_text
        and "implementer=done" in combined_text
    ):
        return None
    request_text = _request_text(value)
    sentinel = _line_value(request_text, "SENTINEL=")
    model = _line_value(request_text, "MODEL_UNDER_TEST=") or _line_value(request_text, "MODEL=")
    endpoint = _line_value(request_text, "ENDPOINT_UNDER_TEST=") or _line_value(request_text, "ENDPOINT=")
    if not sentinel or not model or not endpoint:
        return None
    return "\n".join(
        [
            f"SENTINEL={sentinel}",
            f"MODEL={model}",
            f"ENDPOINT={endpoint}",
            "IMPLEMENTER=done",
        ]
    )


def _looks_like_worker_subagent_prompt(text: str) -> bool:
    if re.search(
        r"\byou are (?:a |an |the )?(?:codex native )?(?:implementer|spec[-_\s]*reviewer|spec compliance reviewer|code[-_\s]*quality reviewer|quality[-_\s]*reviewer)[\w\s_-]{0,80}subagent\b",
        text,
    ):
        return True
    if re.search(
        r"(?m)^\s*role:\s*(?:implementer|spec[-_\s]*reviewer|spec compliance reviewer|code[-_\s]*quality reviewer|quality[-_\s]*reviewer)\b",
        text,
    ):
        return True
    if (
        re.search(
            r"\byou are (?:a |an |the )?(?:spec[-_\s]*reviewer|spec compliance reviewer|code[-_\s]*quality reviewer|quality[-_\s]*reviewer)\b",
            text,
        )
        and ("diagnostic" in text or "artifact" in text or "output_path" in text)
    ):
        return True
    if re.search(r"\byou are implementing task\s+\d+\b", text):
        return True
    if (
        "## task description" in text
        and "## report format" in text
        and "work from:" in text
        and ("do not commit" in text or "do not modify any other files" in text)
    ):
        return True
    if (
        re.search(r"\byour (?:task|job) is to (?:verify|check|review)\b", text)
        and ("diagnostic artifact" in text or "spec compliance" in text or "code quality" in text)
    ):
        return True
    if re.search(r"\byou are (?:reviewing|verifying) task\s+\d+\b", text):
        return True
    return False


def is_worker_subagent_request(value: Any) -> bool:
    text = _active_user_request_text(value) if isinstance(value, list) else _request_text(value)
    return _looks_like_worker_subagent_prompt(text.lower())


def _has_workflow_close_after_wait_intent(value: Any) -> bool:
    text = _request_text(value).lower().replace("-", "_")
    if "close_agent" in text or "multi_agent_v1__close_agent" in text:
        return True
    return any(
        token in text
        for token in (
            "close it",
            "close them",
            "close this agent",
            "close that agent",
            "wait, close",
            "wait and close",
            "wait then close",
        )
    )


def _workflow_task_count(value: Any) -> int | None:
    text = (_workflow_plan_text(value) or _request_text(value)).lower()
    task_numbers = [int(match) for match in re.findall(r"\btask[\s_-]*(\d{1,2})\b", text)]
    task_numbers.extend(int(match) for match in re.findall(r"任务\s*(\d{1,2})", text))
    task_numbers = [number for number in task_numbers if 0 < number <= 20]
    return max(task_numbers) if task_numbers else None


def _workflow_plan_text(value: Any) -> str | None:
    candidates: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, Mapping):
            return
        item_type = item.get("type")
        if item_type in {"message", "function_call_output"}:
            text = _joined_text(item.get("output") if item_type == "function_call_output" else item.get("content"))
            section = _extract_workflow_plan_section(text)
            if section is not None:
                candidates.append(section)
            return
        for child in item.values():
            visit(child)

    visit(value)
    return candidates[-1] if candidates else None


def _extract_workflow_plan_section(text: str) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    heading = "# short subagent development e2e plan"
    heading_index = lowered.rfind(heading)
    if heading_index >= 0:
        return text[heading_index:]
    for marker in ("===== plan =====", "=== plan ==="):
        marker_index = lowered.rfind(marker)
        if marker_index < 0:
            continue
        candidate = text[marker_index + len(marker) :]
        if _looks_like_workflow_plan_text(candidate):
            return candidate
    if _looks_like_workflow_plan_text(text) and re.search(r"\btask[\s_-]*\d{1,2}\b", lowered):
        return text
    return None


def _append_baseline_agent_count(value: Any) -> int | None:
    if not isinstance(value, list):
        return 0

    calls_by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    text_calls_by_id: dict[str, tuple[str, Mapping[str, Any]]] = {}
    agent_ids: list[str] = []
    baseline: int | None = None

    def record_event(event: _Event | None) -> None:
        if event is None or event.kind != "spawn" or not event.agent_id:
            return
        if event.agent_id not in agent_ids:
            agent_ids.append(event.agent_id)

    for item in value:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            tool_name = _multi_agent_tool_name(item)
            call_id = _string_value(item.get("call_id")) or None
            if tool_name and call_id:
                calls_by_id[call_id] = (tool_name, _json_object(item.get("arguments")) or {})
            continue
        if item_type == "function_call_output":
            call_id = _string_value(item.get("call_id")) or None
            if call_id and call_id in calls_by_id:
                tool_name, arguments = calls_by_id[call_id]
                record_event(_event_from_tool_output(call_id, tool_name, arguments, item.get("output")))
            continue
        if item_type == "message":
            if _has_append_intent(item):
                baseline = len(agent_ids)
            text = _joined_text(item.get("content"))
            call_entry = _text_call_entry(text)
            if call_entry is not None:
                call_id, tool_name, arguments = call_entry
                if call_id:
                    text_calls_by_id[call_id] = (tool_name, arguments)
                continue
            record_event(_text_result_event(text, text_calls_by_id))

    return baseline


def _request_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(_request_text(item) for item in value)
    if isinstance(value, Mapping):
        if value.get("type") != "message":
            return ""
        text = _joined_text(value.get("content"))
        first_line = _first_nonempty_line(text) or ""
        if first_line.startswith("Previous real Codex native ") or first_line.startswith("Codex native "):
            return ""
        role = value.get("role")
        return text if role in {"user", "developer", "system"} else ""
    return _joined_text(value)


def _active_request_text(value: Any) -> str:
    if not isinstance(value, list):
        return _request_text(value)
    messages: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        role = item.get("role")
        if role not in {"user", "developer"}:
            continue
        text = _joined_text(item.get("content"))
        first_line = _first_nonempty_line(text) or ""
        if first_line.startswith("Previous real Codex native ") or first_line.startswith("Codex native "):
            continue
        if text.strip():
            messages.append(text)
    return "\n".join(messages) if messages else _request_text(value)


def _active_user_request_text(value: Any) -> str:
    if not isinstance(value, list):
        return _request_text(value)
    messages: list[str] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        if item.get("role") != "user":
            continue
        text = _joined_text(item.get("content"))
        first_line = _first_nonempty_line(text) or ""
        if first_line.startswith("Previous real Codex native ") or first_line.startswith("Codex native "):
            continue
        if text.strip():
            messages.append(text)
    return "\n".join(messages) if messages else _request_text(value)


def _infer_role(text: str) -> str:
    lowered = text.lower().replace("-", "_")
    if re.search(r"\byou are (?:a |an |the )?(?:codex native )?code[_\s]*quality reviewer\b", lowered):
        return "code_quality_reviewer"
    if re.search(r"\byou are (?:a |an |the )?(?:codex native )?(?:spec[_\s]*reviewer|spec compliance reviewer)\b", lowered):
        return "spec_reviewer"
    if re.search(r"\byou are (?:a |an |the )?(?:codex native )?implementer\b", lowered):
        return "implementer"
    if "final" in lowered and "review" in lowered:
        return "final_reviewer"
    if "code_quality" in lowered or "code quality" in lowered or "quality reviewer" in lowered:
        return "code_quality_reviewer"
    if "spec_reviewer" in lowered or "spec reviewer" in lowered or "spec compliance" in lowered:
        return "spec_reviewer"
    if "implementer" in lowered or re.search(r"\bimplement\b", lowered) or "implementation" in lowered:
        return "implementer"
    return "generic"


def _infer_task_key(text: str, role: str) -> str:
    lowered = text.lower()
    if role == "final_reviewer":
        return "all"
    for pattern in (r"task[\s_-]*(\d+)", r"任务\s*(\d+)"):
        match = re.search(pattern, lowered)
        if match:
            return f"task-{int(match.group(1))}"
    if "entire implementation" in lowered or "all tasks" in lowered:
        if role not in {"implementer", "spec_reviewer", "code_quality_reviewer"}:
            return "all"
    if role in {"implementer", "spec_reviewer", "code_quality_reviewer"}:
        return "task-1"
    return "general"


def _normalize_signature(value: str) -> str:
    lowered = value.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9._:-]+", "-", lowered)
    return lowered.strip("-")[:120] or "empty"


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None
    return None


def _completed_agent_results(status: Any) -> dict[str, str]:
    if not isinstance(status, Mapping):
        return {}
    completed: dict[str, str] = {}
    for agent_id, value in status.items():
        if not isinstance(agent_id, str) or not isinstance(value, Mapping):
            continue
        if "completed" in value:
            completed[agent_id] = _joined_text(value.get("completed"))
            continue
        if value.get("status") == "completed":
            completed[agent_id] = _joined_text(
                value.get("message")
                if "message" in value
                else value.get("output")
                if "output" in value
                else value.get("result")
            )
    return completed


def _agent_targets(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(_split_agent_ids(value))
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str) and item)
    return ()


def _split_agent_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def _arguments_block(text: str) -> Mapping[str, Any] | None:
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if line != "arguments:":
            continue
        for candidate in lines[index + 1 :]:
            if not candidate:
                continue
            return _json_object(candidate)
    return None


def _line_value(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def _first_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _joined_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_joined_text(child) for child in value.values())
    if isinstance(value, list):
        return "\n".join(_joined_text(child) for child in value)
    if value is None:
        return ""
    return str(value)


def _string_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()
