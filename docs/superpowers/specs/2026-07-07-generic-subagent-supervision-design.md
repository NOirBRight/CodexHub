# Generic Subagent Supervision Architecture

## Problem

CodexHub's current subagent supervision logic mixes three responsibilities in one state object:

- Protocol lifecycle tracking for `multi_agent_v1` tools.
- Diagnostic gate behavior for bounded one-agent and two-agent E2E scenarios.
- Workflow-specific sequencing for implementers, reviewers, fixes, and task progression.

That coupling causes gate churn. A fix for a higher-level workflow case can change the protocol state machine and regress simpler Level 1 lifecycle behavior. Conversely, a bounded diagnostic repair can hard-code linear behavior that blocks legitimate workflow flexibility.

The new architecture separates protocol supervision from workflow scheduling. The protocol layer must become stable and mostly frozen before workflow scheduling is added above it.

## Goals

- Make the `multi_agent_v1` protocol lifecycle independently testable and stable.
- Support both sequential and parallel subagent workflows.
- Support dynamic workflow scheduling without binding the architecture to Superpowers.
- Prevent workflow failures from causing protocol-layer changes unless a protocol defect is proven.
- Preserve `strict`, `guided`, and `assisted` behavior boundaries.
- Keep CodexHub's Gateway from fabricating final answers or business decisions.

## Non-Goals

- Do not build a Superpowers-specific engine.
- Do not encode reviewer roles, task names, or product diagnostics in the protocol layer.
- Do not make the Gateway choose between multiple valid workflow actions.
- Do not use Level 2 workflow failures as evidence for protocol changes without a protocol-level repro.

## Architecture

The design has four layers.

```text
codex_proxy.py
  Request/response adaptation, tool visibility, event logging.

subagent_policy.py
  strict/guided/assisted policy, repair eligibility, action enforcement rules.

subagent_scheduler.py
  Generic workflow graph, ready action calculation, dynamic task registration.

subagent_protocol.py
  Agent registry, protocol event log, lifecycle state, protocol-safe actions.
```

`codex_proxy.py` should not own workflow semantics. It should ask protocol and scheduler modules what is legal, then apply policy.

## Protocol Layer

The protocol layer owns only facts about native subagent tool lifecycles.

```text
not_spawned
  -> spawned/open
  -> waited/completed
  -> closed

open + wait(empty/null)
  -> needs_input

needs_input + send_input/resume
  -> open
```

Protocol state tracks:

- `agent_id`
- spawn call id
- original prompt
- nickname if present
- lifecycle state
- latest visible result
- empty-output status
- close status

Protocol state does not track:

- implementer or reviewer roles
- task graph dependencies
- exact diagnostic sentinel queues
- plan-read requirements
- Level 1 or Level 2 final formats

The protocol layer outputs protocol actions:

```text
wait(agent_ids)
close(agent_id)
send_input(agent_id, message)
final_allowed
```

It may also report protocol violations:

```text
wait_unknown_agent
wait_closed_agent
close_unknown_agent
close_unwaited_agent
spawn_after_lifecycle_complete
empty_wait_result
```

## Workflow Scheduler Layer

The scheduler layer is generic. It consumes protocol state and workflow state, then returns a set of legal workflow actions.

```text
allowed_actions:
- spawn(node_id, prompt, metadata)
- wait(agent_ids)
- close(agent_id)
- send_input(agent_id, message)
- final
```

The set can contain multiple actions. This is intentional.

Examples:

- Sequential: `spawn A`; after A completes, `spawn B`.
- Parallel: `spawn A`, `spawn B`, and `spawn C` are all legal.
- DAG: after A completes, both `spawn review(A)` and `spawn B` can become legal.
- Dynamic: a coordinator can propose a new task node if it does not violate scheduler constraints.

The scheduler should record workflow nodes separately from protocol agents:

```text
WorkflowNode:
  node_id
  kind
  prompt
  dependencies
  status
  assigned_agent_id
  metadata
```

The scheduler can have adapters for known workflow patterns, but those adapters must live above the generic scheduler. Superpowers can be one adapter, not the scheduler itself.

## Policy Layer

The policy layer decides how to turn legal actions into Gateway behavior.

`strict`:

- Protocol normalization only.
- No semantic action repair.
- No scheduler-driven coercion.

`guided`:

- Inject current protocol and workflow guidance.
- Do not repair model output into required tool calls.

`assisted`:

- Inject guidance.
- Restrict tools when the next action set is unambiguous.
- Repair only when the required tool call and arguments are deterministic.

The key rule:

```text
If exactly one legal action exists and all arguments are known, assisted mode may enforce or repair it.
If multiple legal actions exist, Gateway must guide but not choose.
```

This keeps the workflow flexible while preserving protocol safety.

## Gate Strategy

Gate execution must follow layer boundaries.

```text
P0: protocol unit and transcript contract tests
P1: real single-agent lifecycle E2E
W0: scheduler unit tests using fake protocol state
W1: bounded parallel workflow E2E
W2: dynamic DAG workflow E2E
Product: full assisted model matrix
```

P0 and P1 lock the protocol layer. After P1 is stable, workflow failures cannot change protocol code unless they include a failing P0 or P1 repro.

Failure classification:

```text
protocol_defect
scheduler_defect
policy_defect
adapter_defect
model_choice
provider_stream_flake
diagnostic_parser_bug
```

Only `protocol_defect` can modify `subagent_protocol.py`.

## Protocol Lock Criteria

The protocol layer is considered locked when:

- Protocol unit tests pass.
- Transcript contract tests pass.
- Single-agent real E2E passes at least 95% over a high-repeat run per target model/endpoint.
- Failures are classified and no unclassified protocol transitions remain.

Recommended first stability run:

```text
level1 single-agent only
models: glm52, k2_7, m3
endpoints: responses, chat
repeat: 20
mode: assisted
main retries: 1
```

Generated E2E artifacts remain diagnostics and are not committed by default.

## Migration Plan

1. Extract protocol lifecycle logic from `subagent_state.py` into `subagent_protocol.py`.
2. Add transcript-contract tests that do not call external models.
3. Wire `codex_proxy.py` to protocol outputs without workflow semantics.
4. Run and stabilize P0/P1.
5. Mark protocol files as locked by convention: any future change requires a failing protocol test.
6. Add `subagent_scheduler.py` above the protocol layer.
7. Move bounded two-agent prompt queues into scheduler test fixtures, not protocol.
8. Move reviewer/task sequencing into scheduler adapters.
9. Rebuild Level 2 gates against scheduler behavior.

## Open Design Decisions

- Whether scheduler state is inferred only from transcript or also stored in explicit Gateway metadata.
- Whether dynamic workflow nodes require a model-visible declaration step before spawn.
- Whether workflow adapters should be configured declaratively or implemented as Python strategy classes.

The default implementation should start with transcript-derived state only, then add explicit metadata only if contract tests prove transcript inference is insufficient.
