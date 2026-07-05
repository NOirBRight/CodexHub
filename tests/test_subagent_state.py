import json
import unittest

from subagent_state import build_subagent_state, classify_spawn_request


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


if __name__ == "__main__":
    unittest.main()
