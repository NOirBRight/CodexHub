import json
import unittest

from subagent_protocol import ProtocolEvent, reduce_protocol_events


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


class SubagentProtocolTests(unittest.TestCase):
    def test_single_agent_lifecycle_reaches_closed(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return ok", nickname="child"),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": "ok"}),
                ProtocolEvent.close(call_id="call_close", target="agent-1"),
            ]
        )

        self.assertEqual(state.open_agent_ids, [])
        self.assertEqual(state.waitable_agent_ids, [])
        self.assertEqual(state.closeable_agent_ids, [])
        self.assertEqual(state.closed_agent_ids, ["agent-1"])
        self.assertTrue(state.lifecycle_complete)
        self.assertFalse(state.violations)

    def test_empty_wait_result_requires_input_before_close(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return exact line", nickname=None),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": ""}),
            ]
        )

        self.assertEqual(state.needs_input_agent_ids, ["agent-1"])
        self.assertEqual(state.waitable_agent_ids, [])
        self.assertEqual(state.closeable_agent_ids, [])
        self.assertFalse(state.lifecycle_complete)

    def test_send_input_reopens_empty_wait_agent_for_wait(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return exact line", nickname=None),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": ""}),
                ProtocolEvent.send_input(
                    call_id="call_send", target="agent-1", message="Return the exact requested output."
                ),
            ]
        )

        self.assertEqual(state.needs_input_agent_ids, [])
        self.assertEqual(state.waitable_agent_ids, ["agent-1"])
        self.assertEqual(state.closeable_agent_ids, [])

    def test_close_before_successful_wait_is_violation(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return ok", nickname=None),
                ProtocolEvent.close(call_id="call_close", target="agent-1"),
            ]
        )

        self.assertEqual([violation.code for violation in state.violations], ["close_unwaited_agent"])
        self.assertEqual(state.open_agent_ids, ["agent-1"])
        self.assertEqual(state.closed_agent_ids, [])

    def test_protocol_state_from_responses_structured_items(self):
        from subagent_protocol import protocol_state_from_input_items

        state = protocol_state_from_input_items(
            [
                message("Run one child."),
                call("call_spawn", "spawn_agent", {"message": "return ok", "nickname": "child"}),
                output("call_spawn", {"agent_id": "agent-1", "nickname": "child"}),
                call("call_wait", "wait_agent", {"targets": ["agent-1"], "timeout_ms": 60000}),
                output("call_wait", {"timed_out": False, "status": {"agent-1": {"completed": "ok"}}}),
                call("call_close", "close_agent", {"target": "agent-1"}),
                output("call_close", {"previous_status": {"completed": "ok"}}),
            ]
        )

        self.assertTrue(state.lifecycle_complete)
        self.assertEqual(state.closed_agent_ids, ["agent-1"])

    def test_protocol_parser_accepts_actual_status_message_shape(self):
        from subagent_protocol import protocol_state_from_input_items

        state = protocol_state_from_input_items(
            [
                call("call_spawn", "spawn_agent", {"message": "return ok"}),
                output("call_spawn", {"agent_id": "agent-1"}),
                call("call_wait", "wait_agent", {"targets": ["agent-1"], "timeout_ms": 60000}),
                output(
                    "call_wait",
                    {"timed_out": False, "status": {"agent-1": {"status": "completed", "message": "ok"}}},
                ),
            ]
        )

        self.assertEqual(state.waitable_agent_ids, [])
        self.assertEqual(state.closeable_agent_ids, ["agent-1"])
        self.assertEqual(state.agents["agent-1"].result, "ok")

    def test_wait_unknown_agent_is_protocol_defect_signal(self):
        state = reduce_protocol_events(
            [ProtocolEvent.wait(call_id="call_wait", targets=("missing",), results={"missing": "ok"})]
        )

        self.assertEqual([violation.code for violation in state.violations], ["wait_unknown_agent"])

    def test_close_unknown_agent_is_protocol_defect_signal(self):
        state = reduce_protocol_events([ProtocolEvent.close(call_id="call_close", target="missing")])

        self.assertEqual([violation.code for violation in state.violations], ["close_unknown_agent"])

    def test_wait_closed_agent_is_protocol_defect_signal(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return ok"),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": "ok"}),
                ProtocolEvent.close(call_id="call_close", target="agent-1"),
                ProtocolEvent.wait(call_id="call_wait_again", targets=("agent-1",), results={"agent-1": "ok"}),
            ]
        )

        self.assertEqual([violation.code for violation in state.violations], ["wait_closed_agent"])

    def test_protocol_parser_accepts_developer_text_transcript(self):
        from subagent_protocol import protocol_state_from_input_items

        state = protocol_state_from_input_items(
            [
                {
                    "type": "message",
                    "role": "developer",
                    "content": (
                        "Previous real Codex native multi_agent_v1.spawn_agent call transcript\n"
                        "call_id: call_spawn\n"
                        "arguments:\n"
                        '{"message":"return child-ok"}'
                    ),
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": (
                        "Codex native multi_agent_v1.spawn_agent result\n"
                        "call_id: call_spawn\n"
                        "status: succeeded\n"
                        "agent_id: agent-1\n"
                        "raw_output:\n"
                        '{"agent_id":"agent-1"}'
                    ),
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": (
                        "Previous real Codex native multi_agent_v1.wait_agent call transcript\n"
                        "call_id: call_wait\n"
                        "arguments:\n"
                        '{"targets":["agent-1"],"timeout_ms":60000}'
                    ),
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": (
                        "Codex native multi_agent_v1.wait_agent result\n"
                        "call_id: call_wait\n"
                        "status: completed\n"
                        "completed_agent_ids: agent-1\n"
                        "raw_output:\n"
                        '{"timed_out":false,"status":{"agent-1":{"completed":"child-ok"}}}'
                    ),
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": (
                        "Previous real Codex native multi_agent_v1.close_agent call transcript\n"
                        "call_id: call_close\n"
                        "arguments:\n"
                        '{"target":"agent-1"}'
                    ),
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": (
                        "Codex native multi_agent_v1.close_agent result\n"
                        "call_id: call_close\n"
                        "status: closed\n"
                        "closed_agent_id: agent-1\n"
                        "raw_output:\n"
                        '{"previous_status":{"completed":"child-ok"}}'
                    ),
                },
            ]
        )

        self.assertTrue(state.lifecycle_complete)
        self.assertEqual(state.closed_agent_ids, ["agent-1"])


if __name__ == "__main__":
    unittest.main()
