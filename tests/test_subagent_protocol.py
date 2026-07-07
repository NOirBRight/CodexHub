import unittest

from subagent_protocol import ProtocolEvent, reduce_protocol_events


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


if __name__ == "__main__":
    unittest.main()
