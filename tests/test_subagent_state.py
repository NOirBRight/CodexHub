import json
import unittest

from subagent_state import (
    build_subagent_state,
    classify_spawn_request,
    is_worker_subagent_request,
    state_guidance_message,
)
from subagent_dynamic_dag import LEVEL3_DYNAMIC_DAG_MARKER


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


def spawn(call_id, agent_id, prompt, nickname=None):
    items = [call(call_id, "spawn_agent", {"message": prompt})]
    if nickname is not None:
        items[0]["arguments"]["nickname"] = nickname
    items.append(output(call_id, {"agent_id": agent_id, "nickname": nickname or agent_id}))
    return items


def wait(call_id, targets, completed_text):
    return [
        call(call_id, "wait_agent", {"targets": targets, "timeout_ms": 60000}),
        output(call_id, {"timed_out": False, "status": {target: {"completed": completed_text} for target in targets}}),
    ]


def wait_output(call_id, targets, value):
    return [
        call(call_id, "wait_agent", {"targets": targets, "timeout_ms": 60000}),
        output(call_id, value),
    ]


def close(call_id, target):
    return [
        call(call_id, "close_agent", {"target": target}),
        output(call_id, {"previous_status": {"completed": "ok"}}),
    ]


def send_input(call_id, target, text):
    return [
        call(call_id, "send_input", {"target": target, "message": text}),
        output(call_id, {"status": "sent"}),
    ]


class SubagentStateTests(unittest.TestCase):
    def test_single_lifecycle_complete_blocks_more_spawn(self):
        state = build_subagent_state(
            [
                message("Run exactly one subagent lifecycle: spawn_agent, wait_agent, close_agent."),
                *spawn("call_spawn", "agent-1", "return ok", "child"),
                *wait("call_wait", ["agent-1"], "ok"),
                *close("call_close", "agent-1"),
            ]
        )

        self.assertTrue(state.lifecycle_complete)
        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.next_action, "final")
        self.assertEqual(state.closed_agent_ids, ["agent-1"])
        self.assertFalse(state.allows_spawn_request({"message": "return ok", "nickname": "child"}))
        guidance = state_guidance_message(state)
        self.assertIsNotNone(guidance)
        self.assertIn("visible_response_required", guidance["content"])
        self.assertIn("empty_final_forbidden", guidance["content"])
        self.assertIn("ordinary assistant message content", guidance["content"])
        self.assertIn("first visible output token", guidance["content"])

    def test_bounded_two_spawn_allows_second_spawn_before_wait(self):
        state = build_subagent_state(
            [
                message("Spawn two subagents, then wait for both and close both."),
                *spawn("call_spawn_a", "agent-a", "return A", "a"),
            ]
        )

        self.assertFalse(state.lifecycle_complete)
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.requested_count, 2)
        self.assertEqual(state.wait_agent_ids, ["agent-a"])
        self.assertTrue(state.allows_spawn_request({"message": "return B", "nickname": "b"}))

    def test_bounded_two_spawn_waits_after_requested_spawns_exist(self):
        state = build_subagent_state(
            [
                message("Spawn two subagents, then wait for both and close both."),
                *spawn("call_spawn_a", "agent-a", "return A", "a"),
                *spawn("call_spawn_b", "agent-b", "return B", "b"),
            ]
        )

        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.next_action, "wait")
        self.assertEqual(state.wait_agent_ids, ["agent-a", "agent-b"])

    def test_dynamic_dag_request_sets_workflow_without_plan_read_requirement(self):
        state = build_subagent_state([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER} with native subagents.")])

        self.assertTrue(state.dynamic_dag_intent)
        self.assertTrue(state.workflow_intent)
        self.assertFalse(state.workflow_plan_read)
        self.assertEqual(state.next_action, "spawn")
        self.assertIsNone(state.next_expected_role)
        self.assertIsNone(state.next_expected_task)
        self.assertIsNone(state_guidance_message(state))

    def test_dynamic_dag_waited_node_requires_close(self):
        state = build_subagent_state(
            [
                message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER} with native subagents."),
                *spawn("call_a", "agent-a", "Node: task-a-implementer", "task-a-implementer"),
                *wait("wait_a", ["agent-a"], "A_DONE"),
            ]
        )

        self.assertTrue(state.dynamic_dag_intent)
        self.assertEqual(state.next_action, "close")
        self.assertEqual(state.close_agent_ids, ["agent-a"])

    def test_workflow_rejects_reviewer_spawn_before_implementer(self):
        state = build_subagent_state(
            [
                message(
                    "Use subagent-driven-development: spawn an implementer, then a spec reviewer, then a code quality reviewer."
                ),
            ]
        )

        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.next_expected_role, "implementer")
        self.assertTrue(
            state.allows_spawn_request(
                {"message": "You are the IMPLEMENTER subagent for task 1.", "nickname": "implementer"}
            )
        )
        self.assertFalse(
            state.allows_spawn_request(
                {"message": "You are the SPEC COMPLIANCE REVIEWER subagent for task 1.", "nickname": "spec-reviewer"}
            )
        )

    def test_subagent_development_workflow_ignores_numbered_constraint_counts(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. The implementer creates exactly one diagnostic artifact.
3. The spec reviewer verifies exact file content.
4. The code-quality reviewer verifies minimal implementation.
5. If a reviewer finds issues, route fixes back to the existing implementer path.
6. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        initial = build_subagent_state([message(workflow_prompt)])

        self.assertIsNone(initial.requested_count)
        self.assertFalse(initial.bounded_request)
        self.assertEqual(initial.next_action, "spawn")
        self.assertEqual(initial.next_expected_role, "implementer")

        after_implementer_spawn = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn(
                    "call_impl",
                    "impl-1",
                    "You are the IMPLEMENTER subagent in a diagnostic chain.",
                    "implementer",
                ),
            ]
        )

        self.assertFalse(after_implementer_spawn.should_allow_spawn)
        self.assertEqual(after_implementer_spawn.next_action, "wait")
        self.assertEqual(after_implementer_spawn.wait_agent_ids, ["impl-1"])

        after_implementer_close = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn(
                    "call_impl",
                    "impl-1",
                    "You are the IMPLEMENTER subagent in a diagnostic chain.",
                    "implementer",
                ),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
            ]
        )

        self.assertFalse(after_implementer_close.lifecycle_complete)
        self.assertEqual(after_implementer_close.next_action, "spawn")
        self.assertEqual(after_implementer_close.next_expected_role, "spec_reviewer")

    def test_workflow_failed_implementer_closes_before_retry_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. Start with this ordered lifecycle: spawn one implementer, wait, close; then spawn one spec reviewer, wait, close; then spawn one code-quality reviewer, wait, close.
"""
        after_failed_wait = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn(
                    "call_impl",
                    "impl-1",
                    "You are the IMPLEMENTER subagent in a diagnostic chain.",
                    "implementer",
                ),
                *wait("call_impl_wait", ["impl-1"], "Status: FAILED\nNo file-writing tool was available."),
            ]
        )

        self.assertEqual(after_failed_wait.next_action, "close")
        self.assertEqual(after_failed_wait.close_agent_ids, ["impl-1"])
        self.assertIsNone(after_failed_wait.send_input_target)

        after_failed_close = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn(
                    "call_impl",
                    "impl-1",
                    "You are the IMPLEMENTER subagent in a diagnostic chain.",
                    "implementer",
                ),
                *wait("call_impl_wait", ["impl-1"], "Status: FAILED\nNo file-writing tool was available."),
                *close("call_impl_close", "impl-1"),
            ]
        )

        self.assertEqual(after_failed_close.next_action, "spawn")
        self.assertEqual(after_failed_close.next_expected_role, "implementer")
        self.assertEqual(after_failed_close.next_expected_task, "task-1")

    def test_workflow_requires_plan_read_before_first_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill and this short diagnostic plan:
C:\\repo\\diagnostics\\subagent-e2e-cli\\short-subagent-development-plan.md

Coordinator inputs:
OUTPUT_PATH=C:\\repo\\diagnostics\\artifact.txt
SENTINEL=SENTINEL:level2-m3-chat-20260706
MODEL_UNDER_TEST=minimax-m3
ENDPOINT_UNDER_TEST=chat

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
"""
        state = build_subagent_state([message(workflow_prompt)])
        guidance = state_guidance_message(state)

        self.assertEqual(state.next_action, "spawn")
        self.assertFalse(state.workflow_plan_read)
        self.assertIsNotNone(guidance)
        self.assertIn("status: workflow_plan_read_required", guidance["content"])
        self.assertIn("mcp__node_repl__js", guidance["content"])
        self.assertIn("await import(\"node:fs\")", guidance["content"])

    def test_workflow_guidance_expands_exact_artifact_text_after_plan_read(self):
        workflow_prompt = """
Use the real subagent-driven-development skill and this short diagnostic plan:
C:\\repo\\diagnostics\\subagent-e2e-cli\\short-subagent-development-plan.md

Coordinator inputs:
OUTPUT_PATH=C:\\repo\\diagnostics\\artifact.txt
SENTINEL=SENTINEL:level2-m3-chat-20260706
MODEL_UNDER_TEST=minimax-m3
ENDPOINT_UNDER_TEST=chat

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
"""
        plan_text = """
# Short Subagent Development E2E Plan

The file content must be exactly:

```text
SENTINEL=<SENTINEL>
MODEL=<MODEL_UNDER_TEST>
ENDPOINT=<ENDPOINT_UNDER_TEST>
IMPLEMENTER=done
```

After the implementer reports DONE, run a spec reviewer and then a code quality reviewer.
"""
        state = build_subagent_state(
            [
                message(workflow_prompt),
                {
                    "type": "function_call",
                    "call_id": "call_node_plan",
                    "name": "mcp__node_repl__js",
                    "arguments": json.dumps({"code": "read plan"}),
                },
                {"type": "function_call_output", "call_id": "call_node_plan", "output": plan_text},
            ]
        )
        guidance = state_guidance_message(state)

        self.assertTrue(state.workflow_plan_read)
        self.assertEqual(
            state.workflow_expected_artifact_text,
            "SENTINEL=SENTINEL:level2-m3-chat-20260706\nMODEL=minimax-m3\nENDPOINT=chat\nIMPLEMENTER=done",
        )
        self.assertIsNotNone(guidance)
        self.assertIn("workflow_expected_artifact_exact_text:", guidance["content"])
        self.assertIn("SENTINEL=SENTINEL:level2-m3-chat-20260706", guidance["content"])
        self.assertIn("MODEL=minimax-m3", guidance["content"])
        self.assertIn("Preserve LF newline separators", guidance["content"])
        self.assertIn("concatenated one-line artifact is a failure", guidance["content"])
        self.assertIn("compare raw text or bytes", guidance["content"])

    def test_worker_subagent_prompt_is_not_classified_as_coordinator_workflow(self):
        worker_prompt = r"""
You are a Codex native code-quality reviewer subagent.

READ FIRST:
1. C:\Users\noirb\.codex\plugins\cache\openai-curated-remote\superpowers\5.1.4\skills\subagent-driven-development\SKILL.md

Verify minimal implementation hygiene and absence of product-source modifications for one diagnostic artifact produced by a prior implementer subagent.
The overall chain is implementer, spec reviewer, and code quality reviewer.
"""
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)

    def test_exact_line_child_prompt_is_worker_request(self):
        worker_prompt = "Return exactly this line: SENTINEL:level1-single-glm52-responses"

        self.assertTrue(is_worker_subagent_request([message(worker_prompt)]))
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)

    def test_dynamic_dag_node_prompt_is_worker_request(self):
        worker_prompt = (
            "You are a Level 3 Dynamic DAG worker.\n"
            "Node: task-a-implementer\n"
            "Return exactly one line:\n"
            "A_DONE\n"
            "Do not call multi_agent tools. Do not create or modify files."
        )

        self.assertTrue(is_worker_subagent_request([message(worker_prompt)]))
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)

    def test_worker_implementer_prompt_does_not_request_child_from_artifact_count(self):
        worker_prompt = r"""
You are an implementer subagent. Your task is to create exactly one diagnostic artifact file.

Create the directory structure if it doesn't already exist, then create the file at this exact path:
C:\repo\diagnostics\artifact.txt

Do not modify any other files. Do not create any other files. Use a shell command to create the file.
"""
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)
        self.assertIsNone(state.requested_count)
        self.assertFalse(state.bounded_request)
        self.assertIsNone(state_guidance_message(state))

    def test_task_implementer_worker_prompt_does_not_become_coordinator(self):
        worker_prompt = r"""
You are implementing Task 1: Write The Diagnostic Artifact

## Task Description

Create exactly one text file at the following path:
C:\repo\diagnostics\artifact.txt

## Your Job

1. Create the directory structure if it does not already exist.
2. Do NOT modify any other files.
3. Do NOT commit anything.

Work from: C:\repo

## Report Format

When done, report:
- **Status:** DONE | BLOCKED
"""
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)
        self.assertIsNone(state.requested_count)
        self.assertFalse(state.bounded_request)
        self.assertIsNone(state_guidance_message(state))

    def test_role_header_worker_prompt_does_not_become_coordinator(self):
        worker_prompt = r"""
Role: implementer
Task: Write the diagnostic artifact for the E2E case.

You must create exactly one text file at this path:
C:\repo\diagnostics\artifact.txt
"""
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)
        self.assertIsNone(state.requested_count)
        self.assertIsNone(state_guidance_message(state))

    def test_hyphenated_code_quality_worker_prompt_does_not_become_coordinator(self):
        worker_prompt = r"""
You are the code-quality reviewer subagent for Task 1 of the Short Subagent Development E2E Plan.

Goal: Verify the diagnostic artifact is a minimal implementation.
"""
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)
        self.assertIsNone(state.requested_count)
        self.assertIsNone(state_guidance_message(state))

    def test_diagnostic_reviewer_prompt_without_subagent_word_does_not_become_coordinator(self):
        worker_prompt = r"""
You are a spec compliance reviewer for a diagnostic test. Your job is to verify that a file was created with exact content matching the specification. Do not modify any files.

Check the file at this exact path:
C:\repo\diagnostics\artifact.txt

The required exact content is these four lines, each separated by a newline character (LF), with no BOM.

Report exactly one of:
- PASS - if the file matches the specification exactly
- FAIL - if any discrepancy is found
"""
        state = build_subagent_state([message(worker_prompt)])

        self.assertFalse(state.workflow_intent)
        self.assertIsNone(state.requested_count)
        self.assertIsNone(state_guidance_message(state))

    def test_diagnostic_code_quality_reviewer_without_subagent_word_is_worker_request(self):
        worker_prompt = r"""
You are the code quality reviewer for a small diagnostic E2E case. The implementer was supposed to create exactly one diagnostic artifact and not modify any product source files. Verify both.

## Artifact path
C:\repo\diagnostics\artifact.txt

## What to check
1. Minimal implementation: the artifact exists and contains only the required diagnostic lines.
2. No product source modifications introduced after baseline.
"""

        self.assertTrue(is_worker_subagent_request([message(worker_prompt)]))
        state = build_subagent_state([message(worker_prompt)])
        self.assertFalse(state.workflow_intent)
        self.assertIsNone(state.requested_count)
        self.assertIsNone(state_guidance_message(state))

    def test_level_one_native_lifecycle_prompt_is_not_polluted_by_system_skill_examples(self):
        system_skill_text = """
Use subagent-driven-development with implementer, spec reviewer, and code quality reviewer.
Example Workflow:
Task 1: Hook installation script
Task 2: Recovery modes
"""
        level_one_prompt = """
Execute one real Codex native subagent lifecycle.

You are the coordinator. You must use the visible native subagent tools.

Required sequence:
1. Spawn exactly one child agent.
2. Wait for that child.
3. Close that child.
"""
        state = build_subagent_state(
            [
                {"type": "message", "role": "system", "content": system_skill_text},
                message(level_one_prompt),
            ]
        )

        self.assertFalse(state.workflow_intent)
        self.assertEqual(state.requested_count, 1)
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.next_action, "spawn")

    def test_level_one_native_lifecycle_prompt_is_not_polluted_by_developer_skill_guidance(self):
        developer_skill_text = """
Use the real subagent-driven-development skill.
The coordinator must read the diagnostic plan before spawning.
Roles in this workflow are implementer, spec reviewer, and code quality reviewer.
Use mcp__node_repl__js to read PLAN_PATH before spawning any child agent.
"""
        level_one_prompt = """
Execute a bounded concurrent two-agent Codex native subagent lifecycle.

You are the coordinator. You must use the visible native subagent tools.

Required sequence:
1. Spawn child A with prompt exactly: Return exactly this line: SENTINEL:A
2. Spawn child B with prompt exactly: Return exactly this line: SENTINEL:B
3. Do not wait before both children have been spawned.
4. Wait for both exact child agent ids.
5. Close both exact child agent ids.
"""
        state = build_subagent_state(
            [
                {"type": "message", "role": "developer", "content": developer_skill_text},
                message(level_one_prompt),
            ]
        )
        guidance = state_guidance_message(state)

        self.assertFalse(state.workflow_intent)
        self.assertEqual(state.requested_count, 2)
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.next_action, "spawn")
        self.assertIsNone(guidance)

    def test_workflow_with_close_instruction_closes_implementer_before_reviewer(self):
        workflow_prompt = (
            "Use subagent-driven-development. "
            "Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer."
        )
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn(
                    "call_impl",
                    "impl-1",
                    "You are the IMPLEMENTER subagent in a diagnostic chain.",
                    "implementer",
                ),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
            ]
        )

        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.next_action, "close")
        self.assertEqual(state.close_agent_ids, ["impl-1"])

    def test_workflow_with_wait_close_shorthand_closes_implementer_before_reviewer(self):
        workflow_prompt = (
            "Use subagent-driven-development. "
            "Start with this ordered lifecycle: spawn one implementer, wait, close; then spawn one spec reviewer."
        )
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn(
                    "call_impl",
                    "impl-1",
                    "You are the IMPLEMENTER subagent in a diagnostic chain.",
                    "implementer",
                ),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
            ]
        )

        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.next_action, "close")
        self.assertEqual(state.close_agent_ids, ["impl-1"])

    def test_workflow_incomplete_implementer_closes_before_retry_when_close_required(self):
        workflow_prompt = (
            "Use subagent-driven-development. "
            "Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer."
        )
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait(
                    "call_impl_wait",
                    ["impl-1"],
                    "The file path didn't resolve. Let me check the actual path more carefully.",
                ),
            ]
        )

        self.assertEqual(state.next_action, "close")
        self.assertIsNone(state.send_input_target)
        self.assertEqual(state.close_agent_ids, ["impl-1"])
        self.assertFalse(
            state.allows_spawn_request(
                {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
            )
        )

    def test_workflow_closed_incomplete_implementer_retries_implementer_not_reviewer(self):
        workflow_prompt = (
            "Use subagent-driven-development. "
            "Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer."
        )
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "STATUS: BLOCKED\nCould not find OUTPUT_PATH."),
                *close("call_impl_close", "impl-1"),
            ]
        )

        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.next_expected_role, "implementer")
        self.assertEqual(state.next_expected_task, "task-1")
        self.assertFalse(
            state.allows_spawn_request(
                {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
            )
        )
        self.assertTrue(
            state.allows_spawn_request(
                {"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}
            )
        )

    def test_empty_wait_result_does_not_mark_agent_waited(self):
        workflow_prompt = "Use subagent-driven-development with implementer, spec reviewer, and code quality reviewer."
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait_output("call_impl_wait", ["impl-1"], {"timed_out": False, "status": {}}),
            ]
        )

        self.assertEqual(state.next_action, "wait")
        self.assertEqual(state.wait_agent_ids, ["impl-1"])
        self.assertEqual(state.close_agent_ids, [])

    def test_workflow_completed_null_reviewer_message_requires_send_input_before_rewait(self):
        workflow_prompt = "Use subagent-driven-development with implementer, spec reviewer, and code quality reviewer."
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait_output(
                    "call_spec_wait",
                    ["spec-1"],
                    {
                        "timed_out": False,
                        "status": {
                            "spec-1": {"status": "completed", "message": None},
                        },
                    },
                ),
            ]
        )

        self.assertEqual(state.next_action, "send_input")
        self.assertEqual(state.send_input_target, "spec-1")
        self.assertEqual(state.send_input_reason, "child_empty_output")
        self.assertEqual(state.wait_agent_ids, ["spec-1"])
        self.assertEqual(state.close_agent_ids, [])

    def test_bounded_missing_wait_status_requires_wait_not_final(self):
        state = build_subagent_state(
            [
                message("Spawn one subagent, then wait for it and close it."),
                *spawn("call_a", "agent-a", "Return exactly this line: SENTINEL:A", "child-a"),
                *wait_output("call_wait", ["agent-a"], {"timed_out": False, "status": {}}),
            ]
        )

        self.assertEqual(state.next_action, "wait")
        self.assertEqual(state.wait_agent_ids, ["agent-a"])
        self.assertEqual(state.close_agent_ids, [])
        self.assertFalse(state.lifecycle_complete)

    def test_bounded_empty_completed_wait_result_requires_send_input_before_close(self):
        state = build_subagent_state(
            [
                message("Spawn two subagents, then wait for both and close both."),
                *spawn("call_a", "agent-a", "Return exactly this line: SENTINEL:A", "child-a"),
                *spawn("call_b", "agent-b", "Return exactly this line: SENTINEL:B", "child-b"),
                *wait_output(
                    "call_wait",
                    ["agent-a", "agent-b"],
                    {
                        "timed_out": False,
                        "status": {
                            "agent-a": {"completed": "SENTINEL:A"},
                            "agent-b": {"completed": None},
                        },
                    },
                ),
            ]
        )

        self.assertEqual(state.next_action, "send_input")
        self.assertEqual(state.send_input_target, "agent-b")
        self.assertEqual(state.send_input_reason, "child_empty_output")
        self.assertEqual(state.wait_agent_ids, ["agent-b"])
        self.assertEqual(state.close_agent_ids, ["agent-a"])
        guidance = state_guidance_message(state)
        self.assertIsNotNone(guidance)
        self.assertIn("status: child_empty_output_fix_required", guidance["content"])
        self.assertIn("original_child_prompt: Return exactly this line: SENTINEL:B", guidance["content"])

    def test_bounded_status_completed_null_message_requires_send_input(self):
        state = build_subagent_state(
            [
                message("Spawn two subagents, then wait for both and close both."),
                *spawn("call_a", "agent-a", "Return exactly this line: SENTINEL:A", "child-a"),
                *spawn("call_b", "agent-b", "Return exactly this line: SENTINEL:B", "child-b"),
                *wait_output(
                    "call_wait",
                    ["agent-a", "agent-b"],
                    {
                        "timed_out": False,
                        "status": {
                            "agent-a": {"status": "completed", "message": "SENTINEL:A"},
                            "agent-b": {"status": "completed", "message": None},
                        },
                    },
                ),
            ]
        )

        self.assertEqual(state.next_action, "send_input")
        self.assertEqual(state.send_input_target, "agent-b")

    def test_bounded_child_closes_after_empty_result_send_input_and_wait(self):
        state = build_subagent_state(
            [
                message("Spawn two subagents, then wait for both and close both."),
                *spawn("call_a", "agent-a", "Return exactly this line: SENTINEL:A", "child-a"),
                *spawn("call_b", "agent-b", "Return exactly this line: SENTINEL:B", "child-b"),
                *wait_output(
                    "call_wait",
                    ["agent-a", "agent-b"],
                    {
                        "timed_out": False,
                        "status": {
                            "agent-a": {"completed": "SENTINEL:A"},
                            "agent-b": {"completed": None},
                        },
                    },
                ),
                *send_input("call_send", "agent-b", "Return exactly this line: SENTINEL:B"),
                *wait("call_wait_b", ["agent-b"], "SENTINEL:B"),
            ]
        )

        self.assertEqual(state.next_action, "close")
        self.assertEqual(state.close_agent_ids, ["agent-a", "agent-b"])

    def test_running_wait_result_does_not_mark_agent_waited(self):
        workflow_prompt = "Use subagent-driven-development with implementer, spec reviewer, and code quality reviewer."
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait_output("call_impl_wait", ["impl-1"], {"status": {"impl-1": {"status": "running"}}}),
            ]
        )

        self.assertEqual(state.next_action, "wait")
        self.assertEqual(state.wait_agent_ids, ["impl-1"])
        self.assertEqual(state.close_agent_ids, [])

    def test_workflow_unwaited_quality_reviewer_blocks_additional_spawn(self):
        workflow_prompt = (
            "Use subagent-driven-development.\n\n"
            "## Task 1: Write The Diagnostic Artifact\n"
            "Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, "
            "wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.\n"
            "Final coordinator response must be exactly three lines."
        )
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "You are the IMPLEMENTER subagent in a diagnostic chain.", "implementer"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
                *spawn("call_spec", "spec-1", "You are the SPEC REVIEWER subagent in a diagnostic chain.", "spec-reviewer"),
                *wait("call_spec_wait", ["spec-1"], "PASS"),
                *close("call_spec_close", "spec-1"),
                *spawn(
                    "call_quality",
                    "quality-1",
                    "You are the CODE QUALITY REVIEWER subagent in a diagnostic chain.",
                    "quality-reviewer",
                ),
            ]
        )

        self.assertEqual(state.next_action, "wait")
        self.assertEqual(state.wait_agent_ids, ["quality-1"])
        self.assertFalse(state.should_allow_spawn)

    def test_single_task_workflow_finalizes_after_quality_reviewer_close(self):
        workflow_prompt = (
            "Use subagent-driven-development.\n\n"
            "## Task 1: Write The Diagnostic Artifact\n"
            "Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, "
            "wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.\n"
            "Final coordinator response must be exactly three lines."
        )
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "You are the IMPLEMENTER subagent in a diagnostic chain.", "implementer"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
                *spawn("call_spec", "spec-1", "You are the SPEC REVIEWER subagent in a diagnostic chain.", "spec-reviewer"),
                *wait("call_spec_wait", ["spec-1"], "PASS"),
                *close("call_spec_close", "spec-1"),
                *spawn(
                    "call_quality",
                    "quality-1",
                    "You are the CODE QUALITY REVIEWER subagent in a diagnostic chain.",
                    "quality-reviewer",
                ),
                *wait("call_quality_wait", ["quality-1"], "PASS"),
                *close("call_quality_close", "quality-1"),
            ]
        )

        self.assertTrue(state.lifecycle_complete)
        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.next_action, "final")

    def test_single_task_workflow_closes_quality_reviewer_before_finalizing(self):
        workflow_prompt = (
            "Use subagent-driven-development.\n\n"
            "## Task 1: Write The Diagnostic Artifact\n"
            "Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, "
            "wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.\n"
            "Final coordinator response must be exactly three lines."
        )
        state = build_subagent_state(
            [
                message(workflow_prompt),
                *spawn("call_impl", "impl-1", "You are the IMPLEMENTER subagent in a diagnostic chain.", "implementer"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
                *spawn("call_spec", "spec-1", "You are the SPEC REVIEWER subagent in a diagnostic chain.", "spec-reviewer"),
                *wait("call_spec_wait", ["spec-1"], "PASS"),
                *close("call_spec_close", "spec-1"),
                *spawn(
                    "call_quality",
                    "quality-1",
                    "You are the CODE QUALITY REVIEWER subagent in a diagnostic chain.",
                    "quality-reviewer",
                ),
                *wait("call_quality_wait", ["quality-1"], "PASS"),
            ]
        )

        self.assertFalse(state.lifecycle_complete)
        self.assertEqual(state.next_action, "close")
        self.assertEqual(state.close_agent_ids, ["quality-1"])

    def test_workflow_task_count_comes_from_real_plan_read_output_not_skill_examples(self):
        workflow_prompt = (
            "Use subagent-driven-development with the short diagnostic plan at "
            "diagnostics/subagent-e2e-cli/short-subagent-development-plan.md."
        )
        plan_read = {
            "type": "function_call_output",
            "call_id": "call_plan_read",
            "output": json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "=== SKILL ===\n"
                                "Example Workflow\n"
                                "Task 1: Hook installation script\n"
                                "Task 2: Recovery modes\n"
                                "=== PLAN ===\n"
                                "# Short Subagent Development E2E Plan\n\n"
                                "## Task 1: Write The Diagnostic Artifact\n\n"
                                "Use a fresh implementer subagent. After the implementer reports DONE, "
                                "run a spec compliance reviewer subagent. After the spec reviewer reports pass, "
                                "run a code quality reviewer subagent.\n\n"
                                "Final coordinator response must be exactly three lines."
                            ),
                        }
                    ]
                }
            ),
        }
        state = build_subagent_state(
            [
                message(workflow_prompt),
                {
                    "type": "function_call",
                    "call_id": "call_plan_read",
                    "namespace": "mcp__node_repl",
                    "name": "js",
                    "arguments": {"code": "read plan"},
                },
                plan_read,
                *spawn("call_impl", "impl-1", "You are the IMPLEMENTER subagent in a diagnostic chain.", "implementer"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
                *spawn("call_spec", "spec-1", "You are the SPEC REVIEWER subagent in a diagnostic chain.", "spec-reviewer"),
                *wait("call_spec_wait", ["spec-1"], "PASS"),
                *close("call_spec_close", "spec-1"),
                *spawn(
                    "call_quality",
                    "quality-1",
                    "You are the CODE QUALITY REVIEWER subagent in a diagnostic chain.",
                    "quality-reviewer",
                ),
                *wait("call_quality_wait", ["quality-1"], "PASS"),
                *close("call_quality_close", "quality-1"),
            ]
        )

        self.assertEqual(state.workflow_task_count, 1)
        self.assertTrue(state.lifecycle_complete)
        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.next_action, "final")

    def test_append_intent_allows_one_more_spawn_after_latest_request(self):
        state = build_subagent_state(
            [
                *spawn("call_spawn_a", "agent-a", "return A", "a"),
                message("Spawn another subagent before waiting for the existing child."),
            ]
        )

        self.assertTrue(state.requested_append)
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.next_action, "spawn")
        self.assertTrue(state.allows_spawn_request({"message": "return B", "nickname": "b"}))

    def test_append_intent_waits_after_one_additional_spawn_exists(self):
        state = build_subagent_state(
            [
                *spawn("call_spawn_a", "agent-a", "return A", "a"),
                message("Spawn another subagent before waiting for the existing child."),
                *spawn("call_spawn_b", "agent-b", "return B", "b"),
            ]
        )

        self.assertFalse(state.should_allow_spawn)
        self.assertEqual(state.next_action, "wait")
        self.assertEqual(state.wait_agent_ids, ["agent-a", "agent-b"])

    def test_repeated_append_intent_uses_latest_request_as_baseline(self):
        state = build_subagent_state(
            [
                *spawn("call_spawn_a", "agent-a", "return A", "a"),
                message("Spawn another subagent before waiting for the existing child."),
                *spawn("call_spawn_b", "agent-b", "return B", "b"),
                message("Spawn another subagent before waiting for the existing children."),
            ]
        )

        self.assertTrue(state.requested_append)
        self.assertTrue(state.should_allow_spawn)
        self.assertEqual(state.next_action, "spawn")
        self.assertTrue(state.allows_spawn_request({"message": "return B", "nickname": "b"}))

    def test_duplicate_implementer_same_epoch_is_blocked(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
            ]
        )

        self.assertEqual(state.next_expected_role, "spec_reviewer")
        self.assertFalse(
            state.allows_spawn_request(
                {"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}
            )
        )
        self.assertTrue(
            state.allows_spawn_request(
                {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
            )
        )

    def test_reviewer_issue_routes_back_to_existing_implementer(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait("call_spec_wait", ["spec-1"], "ISSUE: missing required test"),
            ]
        )

        self.assertEqual(state.next_action, "send_input")
        self.assertEqual(state.send_input_target, "impl-1")
        self.assertFalse(
            state.allows_spawn_request(
                {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
            )
        )

    def test_reviewer_issue_after_closed_implementer_spawns_new_implementer_fix_round(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait("call_spec_wait", ["spec-1"], "ISSUE: missing required test"),
            ]
        )

        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.next_expected_role, "implementer")
        self.assertEqual(state.next_expected_task, "task-1")
        self.assertIsNone(state.send_input_target)
        self.assertFalse(
            state.allows_spawn_request(
                {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
            )
        )
        self.assertTrue(
            state.allows_spawn_request(
                {"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}
            )
        )

    def test_resume_agent_id_argument_advances_implementation_epoch_after_fix(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *close("call_impl_close", "impl-1"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait("call_spec_wait", ["spec-1"], "ISSUE: missing required test"),
                call("call_resume", "resume_agent", {"id": "impl-1"}),
                output("call_resume", {"status": "resumed"}),
                *wait("call_impl_wait_2", ["impl-1"], "DONE fixed"),
            ]
        )

        self.assertEqual(state.implementation_epoch_by_task["task-1"], 2)
        self.assertEqual(state.next_expected_role, "spec_reviewer")
        self.assertTrue(
            state.allows_spawn_request(
                {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
            )
        )

    def test_implementer_fix_advances_epoch_and_allows_same_spec_reviewer_again(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait("call_spec_wait", ["spec-1"], "ISSUE: missing required test"),
                *send_input("call_send", "impl-1", "Fix missing required test."),
                *wait("call_impl_wait_2", ["impl-1"], "DONE fixed"),
            ]
        )

        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.next_expected_role, "spec_reviewer")
        self.assertEqual(state.implementation_epoch_by_task["task-1"], 2)
        self.assertTrue(
            state.allows_spawn_request(
                {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
            )
        )

    def test_spec_pass_allows_quality_reviewer_then_next_task_implementer(self):
        after_spec_pass = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait("call_spec_wait", ["spec-1"], "PASS compliant"),
            ]
        )

        self.assertEqual(after_spec_pass.next_expected_role, "code_quality_reviewer")
        self.assertTrue(
            after_spec_pass.allows_spawn_request(
                {"message": "Code quality review for Task 1.", "nickname": "code-quality-reviewer-task-1"}
            )
        )

        after_quality_pass = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait("call_spec_wait", ["spec-1"], "PASS compliant"),
                *spawn("call_quality", "quality-1", "Code quality review for Task 1.", "code-quality-reviewer-task-1"),
                *wait("call_quality_wait", ["quality-1"], "APPROVED"),
            ]
        )

        self.assertEqual(after_quality_pass.next_expected_role, "implementer")
        self.assertEqual(after_quality_pass.next_expected_task, "task-2")
        self.assertTrue(
            after_quality_pass.allows_spawn_request(
                {"message": "Implement Task 2 exactly.", "nickname": "implementer-task-2"}
            )
        )

    def test_review_pass_with_no_issues_is_not_treated_as_failure(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait("call_spec_wait", ["spec-1"], "PASS: no issues found"),
            ]
        )

        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.next_expected_role, "code_quality_reviewer")
        self.assertIsNone(state.send_input_target)

    def test_explicit_pass_report_with_failure_context_is_not_treated_as_failure(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait(
                    "call_spec_wait",
                    ["spec-1"],
                    """
# SPEC REVIEWER - Verification Report

**Status: PASS**

File read back exactly and all required content matches.

If FAIL, state which line/byte mismatched.

**Verdict: PASS**
""",
                ),
            ]
        )

        self.assertEqual(state.next_action, "spawn")
        self.assertEqual(state.next_expected_role, "code_quality_reviewer")
        self.assertIsNone(state.send_input_target)

    def test_explicit_fail_report_still_routes_back_to_implementer(self):
        state = build_subagent_state(
            [
                *spawn("call_impl", "impl-1", "Implement Task 1 exactly.", "implementer-task-1"),
                *wait("call_impl_wait", ["impl-1"], "DONE"),
                *spawn("call_spec", "spec-1", "Spec compliance review for Task 1.", "spec-reviewer-task-1"),
                *wait(
                    "call_spec_wait",
                    ["spec-1"],
                    """
# SPEC REVIEWER - Verification Report

**Status: FAIL**

Missing required sentinel line.
""",
                ),
            ]
        )

        self.assertEqual(state.next_action, "send_input")
        self.assertEqual(state.send_input_target, "impl-1")

    def test_classifies_spawn_request_from_prompt_and_nickname(self):
        request = classify_spawn_request(
            {
                "message": "Please perform a spec compliance review for Task 3.",
                "nickname": "spec-reviewer-task-3",
            }
        )

        self.assertEqual(request.role, "spec_reviewer")
        self.assertEqual(request.task_key, "task-3")
        self.assertEqual(request.signature, "spec_reviewer:task-3")

    def test_explicit_implementer_fix_role_wins_over_spec_issue_text(self):
        request = classify_spawn_request(
            {
                "message": (
                    "You are an implementer subagent fixing a spec compliance issue. "
                    "Rewrite the diagnostic artifact exactly."
                ),
                "nickname": "implementer-fix",
            }
        )

        self.assertEqual(request.role, "implementer")
        self.assertEqual(request.task_key, "task-1")

    def test_workflow_role_without_explicit_task_defaults_to_task_one(self):
        request = classify_spawn_request(
            {
                "message": "You are the IMPLEMENTER subagent in a diagnostic chain.",
                "nickname": "implementer",
            }
        )

        self.assertEqual(request.role, "implementer")
        self.assertEqual(request.task_key, "task-1")
        self.assertEqual(request.signature, "implementer:task-1")

    def test_workflow_implementer_all_tasks_defaults_to_task_one(self):
        request = classify_spawn_request(
            {
                "message": "You are the IMPLEMENTER subagent. Implement all tasks in this short diagnostic plan.",
                "nickname": "implementer",
            }
        )

        self.assertEqual(request.role, "implementer")
        self.assertEqual(request.task_key, "task-1")
        self.assertEqual(request.signature, "implementer:task-1")

    def test_final_reviewer_still_targets_all_tasks(self):
        request = classify_spawn_request(
            {
                "message": "You are the FINAL REVIEWER. Review all tasks and the entire implementation.",
                "nickname": "final-reviewer",
            }
        )

        self.assertEqual(request.role, "final_reviewer")
        self.assertEqual(request.task_key, "all")

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


if __name__ == "__main__":
    unittest.main()
