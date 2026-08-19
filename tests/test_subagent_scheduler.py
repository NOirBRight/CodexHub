import unittest

from subagent_protocol import ProtocolEvent, reduce_protocol_events
from subagent_scheduler import (
    WorkflowNode,
    WorkflowState,
    compute_allowed_actions,
    node_complete,
    workflow_complete,
)


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

    def test_closed_dependency_releases_multiple_ready_nodes(self):
        workflow = WorkflowState(
            nodes={
                "task-a": WorkflowNode(
                    node_id="task-a",
                    prompt="do A",
                    assigned_agent_id="agent-a",
                ),
                "review-a": WorkflowNode(node_id="review-a", prompt="review A", dependencies=("task-a",)),
                "task-b": WorkflowNode(node_id="task-b", prompt="do B", dependencies=("task-a",)),
            }
        )
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


if __name__ == "__main__":
    unittest.main()
