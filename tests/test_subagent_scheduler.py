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


if __name__ == "__main__":
    unittest.main()
