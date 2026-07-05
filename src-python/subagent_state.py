from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


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


@dataclass
class SubagentState:
    agents: dict[str, AgentState] = field(default_factory=dict)
    requested_count: int | None = None
    requested_append: bool = False
    bounded_request: bool = False
    implementation_epoch_by_task: dict[str, int] = field(default_factory=dict)
    pending_fix_targets: set[str] = field(default_factory=set)
    close_waited_agents: bool = False
    next_action: str = "spawn"
    next_expected_role: str | None = None
    next_expected_task: str | None = None
    send_input_target: str | None = None
    lifecycle_complete: bool = False

    @property
    def spawned_agent_ids(self) -> list[str]:
        return list(self.agents.keys())

    @property
    def closed_agent_ids(self) -> list[str]:
        return [agent.agent_id for agent in self.agents.values() if agent.closed]

    @property
    def open_agent_ids(self) -> list[str]:
        return [agent.agent_id for agent in self.agents.values() if not agent.closed]

    @property
    def wait_agent_ids(self) -> list[str]:
        return [agent.agent_id for agent in self.agents.values() if not agent.closed and not agent.waited]

    @property
    def close_agent_ids(self) -> list[str]:
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
        return _allows_spawn_request(self.agents.values(), request, self.implementation_epoch_by_task)


@dataclass(frozen=True)
class _Event:
    kind: str
    call_id: str | None = None
    agent_id: str | None = None
    arguments: Mapping[str, Any] | None = None
    result: str = ""
    targets: tuple[str, ...] = ()
    target: str | None = None


def build_subagent_state(input_items: Any) -> SubagentState:
    events = _events_from_items(input_items)
    requested_count = _requested_spawn_count(input_items)
    requested_append = _has_append_intent(input_items)
    agents, epochs, pending_fix_targets = _apply_events(events)
    state = SubagentState(
        agents=agents,
        requested_count=requested_count,
        requested_append=requested_append,
        bounded_request=requested_count is not None,
        implementation_epoch_by_task=epochs,
        pending_fix_targets=pending_fix_targets,
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
            "required_next_action: write the final concise report now. The requested subagent lifecycle is already complete."
        )
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
        lines.append(
            "required_next_action: call multi_agent_v1__spawn_agent for this distinct role/task. Do not repeat an already spawned role/task in the same implementation epoch."
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
        lines.append("required_next_action: call multi_agent_v1__close_agent for one completed agent_id.")
        return _developer_text_message("\n".join(lines))

    if state.next_action == "send_input" and state.send_input_target:
        lines.append("status: reviewer_issue_fix_required")
        lines.append(f"send_input_target: {state.send_input_target}")
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
                if agent_id in pending_fix_targets and agent.role == "implementer":
                    epochs[agent.task_key] = max(epochs.get(agent.task_key, agent.epoch), agent.epoch) + 1
                    agent.epoch = epochs[agent.task_key]
                    pending_fix_targets.discard(agent_id)
                agent.waited = True
                agent.result = event.result
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

    return agents, epochs, pending_fix_targets


def _compute_next_action(state: SubagentState) -> None:
    if state.bounded_request:
        state.close_waited_agents = True
        if state.requested_count is not None and len(state.agents) < state.requested_count:
            state.next_action = "spawn"
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

    if state.agents and not _has_workflow_agents(state.agents.values()):
        state.close_waited_agents = True
        if state.wait_agent_ids:
            state.next_action = "wait"
            return
        if state.close_agent_ids:
            state.next_action = "close"
            return
        state.next_action = "spawn"
        return

    pending_target = _pending_fix_target(state)
    if pending_target:
        state.next_action = "wait"
        state.send_input_target = pending_target
        return

    failed_review = _latest_failed_review(state.agents.values())
    if failed_review is not None:
        implementer = _latest_implementer(state.agents.values(), failed_review.task_key)
        if implementer is not None and not implementer.closed:
            state.next_action = "send_input"
            state.send_input_target = implementer.agent_id
            return

    task = _latest_current_task(state.agents.values())
    implementer = _latest_implementer(state.agents.values(), task)
    if implementer is None:
        state.next_action = "spawn"
        state.next_expected_role = "implementer"
        state.next_expected_task = task
        return
    if not implementer.waited:
        state.next_action = "wait"
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

    state.next_action = "spawn"
    state.next_expected_role = "implementer"
    state.next_expected_task = _next_task_key(task)


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


def _pending_fix_target(state: SubagentState) -> str | None:
    for target in state.pending_fix_targets:
        agent = state.agents.get(target)
        if agent is not None and not agent.closed:
            return target
    return None


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


def _contains_issue(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("issue", "missing", "not compliant", "fail", "failed", "问题", "缺失"))


def _contains_pass(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("pass", "approved", "compliant", "✅", "通过", "批准"))


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
        completed = _completed_agent_ids(output_obj.get("status"))
        if completed:
            targets = tuple(agent_id for agent_id in targets if agent_id in completed) or completed
        return _Event("wait", call_id=call_id, arguments=arguments, result=result, targets=targets)
    if tool_name == "close_agent":
        target = _string_value(arguments.get("target"))
        return _Event("close", call_id=call_id, arguments=arguments, target=target or None, targets=((target,) if target else ()))
    if tool_name in {"send_input", "resume_agent"}:
        target = _string_value(arguments.get("target") or arguments.get("agent_id"))
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
    text = _joined_text(value).lower()
    if not any(token in text for token in ("spawn", "subagent", "sub-agent", "agent", "子代理", "multi_agent")):
        return None

    for pattern in (
        r"(?:spawn|spawns|create|start|launch|创建|启动|派发|调用|开|生成)\s*(?<!第)(\d{1,2})\s*(?:个|名|位)?\s*(?:sub-?agents?|agents?|子代理)",
        r"(?<!第)(\d{1,2})\s*(?:个|名|位)?\s*(?:sub-?agents?|agents?|子代理)",
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
    match = re.search(rf"(?<!第)({pattern})\s*(?:个|名|位)?\s*子代理", text)
    if match:
        return word_numbers[match.group(1)]
    if any(token in text for token in ("exactly one", "only one", "执行一次真实", "只执行一次", "一次真实", "一个子代理")):
        return 1
    return None


def _has_append_intent(value: Any) -> bool:
    text = _joined_text(value).lower()
    return any(token in text for token in ("another", "second", "next child", "再", "另一个", "第二个", "追加"))


def _infer_role(text: str) -> str:
    lowered = text.lower().replace("-", "_")
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
    if role == "final_reviewer" or "entire implementation" in lowered or "all tasks" in lowered:
        return "all"
    for pattern in (r"task[\s_-]*(\d+)", r"任务\s*(\d+)"):
        match = re.search(pattern, lowered)
        if match:
            return f"task-{int(match.group(1))}"
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


def _completed_agent_ids(status: Any) -> tuple[str, ...]:
    if not isinstance(status, Mapping):
        return ()
    return tuple(
        agent_id
        for agent_id, value in status.items()
        if isinstance(agent_id, str) and isinstance(value, Mapping) and "completed" in value
    )


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
