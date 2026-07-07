# Level3 Dynamic DAG Workflow Design

## Problem

Level 1 now covers native subagent protocol lifecycle stability. Level 2 covers a fixed ordered workflow: implementer, spec reviewer, code-quality reviewer, with a focused repair loop.

Level 3 must prove the workflow layer can adapt at runtime without weakening the protocol lock. The target behavior is a dynamic DAG: after one node completes, the coordinator can unlock or append multiple next nodes, including parallel work and review work that was not a fixed linear sequence at the start.

## Goals

- Support runtime workflow graph changes above the protocol layer.
- Allow multiple ready spawn actions when the DAG permits parallel work.
- Keep assisted repair deterministic: repair only when exactly one legal action exists.
- Preserve Level 1 protocol behavior and Level 2 ordered workflow behavior.
- Provide an E2E gate that distinguishes scheduler defects from protocol defects.
- Keep the framework generic, not tied to Superpowers-specific roles.

## Non-Goals

- Do not rewrite the protocol lifecycle state machine.
- Do not move business workflow semantics into `subagent_protocol.py`.
- Do not let the Gateway synthesize final answers or reviewer decisions.
- Do not make the Gateway choose between multiple valid DAG branches.
- Do not build a full planner language in the first Level 3 slice.
- Do not require every workflow to be dynamic; fixed ordered workflows remain supported.

## Architecture

Level 3 adds a dynamic workflow adapter on top of the existing scheduler primitive.

```text
subagent_protocol.py
  Lifecycle facts only: spawned, waitable, closeable, closed, needs_input.

subagent_scheduler.py
  Generic DAG state and legal action calculation.

subagent_state.py
  Workflow adapter selection and state summarization for Gateway guidance.

codex_proxy.py
  Tool visibility, deterministic repair, response suppression, event logging.
```

The core boundary remains:

```text
Protocol facts -> Scheduler legal actions -> Gateway policy
```

If a Level 3 failure occurs, it can change protocol code only when a protocol-level unit test or Level 1 repro proves a lifecycle fact is wrong.

## Dynamic DAG Model

The scheduler owns generic nodes:

```text
WorkflowNode:
  node_id
  prompt
  dependencies
  assigned_agent_id
  status
  metadata
```

Level 3 needs three additions to the current static scheduler:

- `append_node(node)`: add a new node at runtime after validating dependency ids exist or are explicitly external.
- `ready_spawn_actions()`: return all unassigned nodes whose dependencies are complete.
- `workflow_complete`: true when no open protocol work remains and every required terminal node is complete.

Node completion is still derived from protocol state: a node assigned to `agent_id` is complete only after that agent has been waited and closed.

## Gateway Policy

The Gateway consumes scheduler actions under the existing assist modes.

`strict`:

- Protocol normalization only.
- No workflow action repair.

`guided`:

- Inject DAG state guidance.
- Do not repair model output into scheduler actions.

`assisted`:

- If exactly one legal action exists and arguments are complete, repair or coerce to that action.
- If multiple legal spawn actions exist, expose spawn and guide the model, but do not choose which node to spawn.
- If multiple ready nodes are spawned in the same turn and each maps to a distinct legal node, accept them.
- Suppress duplicate spawns for an already assigned node.
- Preserve post-final tool-call suppression.

## Minimum Level 3 E2E Scenario

The first Dynamic DAG E2E should be deterministic but genuinely dynamic.

Initial graph:

```text
task-a-implementer
```

Runtime transition:

```text
task-a-implementer closes
  -> append task-a-reviewer
  -> append task-b-implementer
```

Parallel phase:

```text
task-a-reviewer and task-b-implementer are both ready.
The coordinator may spawn them in either order or in the same turn.
```

Final phase:

```text
task-a-reviewer closes
task-b-implementer closes
  -> append final-summarizer
```

Required final output:

```text
RESULT: PASS
DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer
DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done
```

The E2E parser should accept either spawn order for the parallel phase, but it must reject:

- final before all terminal dependencies complete
- duplicate node spawns
- direct artifact or verification work by the coordinator
- local/MCP tool calls by the coordinator after child lifecycle begins
- nested multi-agent tool use inside worker subagents

## Unit Test Plan

Scheduler tests:

- A closed dependency releases multiple ready nodes.
- Runtime `append_node` rejects duplicate node ids.
- Runtime `append_node` rejects missing dependencies unless marked external.
- Assigned nodes are not returned as ready again.
- Multiple ready nodes produce multiple legal spawn actions.
- Protocol wait/close actions take precedence for assigned open nodes.
- Final action appears only when terminal dynamic nodes are complete.

Gateway tests:

- Multiple legal DAG spawn actions do not trigger deterministic repair.
- Distinct legal DAG spawns in one response are accepted.
- Duplicate spawn for an assigned DAG node is suppressed.
- A single legal DAG action can be repaired in assisted mode.
- Guided mode injects DAG state without coercion.
- Post-final multi-agent calls remain suppressed.

E2E parser tests:

- Accept either order for `task-a-reviewer` and `task-b-implementer`.
- Require final summarizer after both dynamic branch nodes close.
- Classify dynamic scheduler failures separately from protocol failures.

## Gate Strategy

Run gates in this order:

1. Scheduler unit tests only.
2. Gateway unit tests for legal-action exposure and repair boundaries.
3. Focused Level 3 E2E on one model and one endpoint.
4. Focused Level 3 E2E on all three target models for one endpoint.
5. Full Level 3 matrix only after focused runs are clean.

Recommended initial full gate:

```text
--level level3
--workflow dynamic-dag
--models glm52,k2_7,m3
--endpoints responses,chat
--jobs 3
--repeat 3
--subagent-mode assisted
--main-retry-attempts 1
```

## Completion Criteria

Level 3 is complete when:

- Existing Level 1 and Level 2 focused regression tests still pass.
- Dynamic DAG scheduler unit tests pass.
- Gateway Dynamic DAG repair/guidance tests pass.
- Level 3 focused E2E passes for `glm52`, `k2_7`, and `m3`.
- Full Level 3 assisted E2E passes or any failure is classified as provider flake with artifact, lifecycle, and final checks correct.

## Open Implementation Constraint

The first implementation should be small. It should not replace the current Level 2 ordered workflow code. It should add a Dynamic DAG path that can run beside Level 2. Shared helpers can be extracted only after both paths have passing tests that expose identical behavior.
