import unittest

from subagent_dynamic_dag import (
    LEVEL3_DYNAMIC_DAG_MARKER,
    build_dynamic_dag_workflow,
    dynamic_dag_guidance_message,
    is_dynamic_dag_request,
)
from subagent_protocol import ProtocolEvent, reduce_protocol_events


def message(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


class DynamicDagAdapterTests(unittest.TestCase):
    def test_detects_dynamic_dag_request_marker(self):
        self.assertTrue(is_dynamic_dag_request([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")]))
        self.assertFalse(is_dynamic_dag_request([message("Run an ordered Level 2 workflow.")]))

    def test_initial_workflow_contains_only_task_a_implementer(self):
        protocol = reduce_protocol_events([])

        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        self.assertEqual(list(workflow.nodes), ["task-a-implementer"])
        self.assertEqual(workflow.nodes["task-a-implementer"].dependencies, ())

    def test_task_a_close_appends_review_and_task_b(self):
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_a", "agent-a", "Node: task-a-implementer", "task-a-implementer"),
                ProtocolEvent.wait("wait_a", ("agent-a",), {"agent-a": "A_DONE"}),
                ProtocolEvent.close("close_a", "agent-a"),
            ]
        )

        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        self.assertEqual(
            list(workflow.nodes),
            ["task-a-implementer", "task-a-reviewer", "task-b-implementer"],
        )
        self.assertEqual(workflow.nodes["task-a-reviewer"].dependencies, ("task-a-implementer",))
        self.assertEqual(workflow.nodes["task-b-implementer"].dependencies, ("task-a-implementer",))
        self.assertEqual(workflow.nodes["task-a-implementer"].assigned_agent_id, "agent-a")

    def test_branch_closes_append_terminal_final_summarizer(self):
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_a", "agent-a", "Node: task-a-implementer", "task-a-implementer"),
                ProtocolEvent.wait("wait_a", ("agent-a",), {"agent-a": "A_DONE"}),
                ProtocolEvent.close("close_a", "agent-a"),
                ProtocolEvent.spawn("call_review", "agent-review", "Node: task-a-reviewer", "task-a-reviewer"),
                ProtocolEvent.wait("wait_review", ("agent-review",), {"agent-review": "A_REVIEW_PASS"}),
                ProtocolEvent.close("close_review", "agent-review"),
                ProtocolEvent.spawn("call_b", "agent-b", "Node: task-b-implementer", "task-b-implementer"),
                ProtocolEvent.wait("wait_b", ("agent-b",), {"agent-b": "B_DONE"}),
                ProtocolEvent.close("close_b", "agent-b"),
            ]
        )

        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        self.assertIn("final-summarizer", workflow.nodes)
        self.assertEqual(
            workflow.nodes["final-summarizer"].dependencies,
            ("task-a-reviewer", "task-b-implementer"),
        )
        self.assertTrue(workflow.nodes["final-summarizer"].terminal)

    def test_guidance_lists_ready_dynamic_nodes(self):
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_a", "agent-a", "Node: task-a-implementer", "task-a-implementer"),
                ProtocolEvent.wait("wait_a", ("agent-a",), {"agent-a": "A_DONE"}),
                ProtocolEvent.close("close_a", "agent-a"),
            ]
        )
        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        guidance = dynamic_dag_guidance_message(workflow, protocol)
        text = guidance["content"][0]["text"]

        self.assertIn("workflow_type: dynamic_dag", text)
        self.assertIn("ready_nodes: task-a-reviewer, task-b-implementer", text)


if __name__ == "__main__":
    unittest.main()
