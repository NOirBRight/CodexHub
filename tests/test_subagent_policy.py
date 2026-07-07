import os
import unittest
from unittest.mock import patch

from subagent_policy import (
    deterministic_required_action,
    guidance_enabled,
    semantic_repair_enabled,
    subagent_assist_mode,
)


class SubagentPolicyTests(unittest.TestCase):
    def test_assist_mode_defaults_to_assisted(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(subagent_assist_mode(), "assisted")

    def test_guided_has_guidance_without_repair(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "guided"}, clear=False):
            self.assertTrue(guidance_enabled({}))
            self.assertFalse(semantic_repair_enabled({}))

    def test_raw_probe_disables_guidance_and_repair(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            self.assertFalse(guidance_enabled({"raw_provider_probe": True}))
            self.assertFalse(semantic_repair_enabled({"raw_provider_probe": True}))

    def test_deterministic_required_action_returns_single_known_action(self):
        action = {"kind": "protocol", "tool_name": "wait_agent", "arguments": {"targets": ["agent-1"]}}
        self.assertEqual(deterministic_required_action([action]), action)

    def test_deterministic_required_action_refuses_multiple_valid_actions(self):
        actions = [
            {"kind": "workflow", "tool_name": "spawn_agent", "arguments": {"message": "task B"}},
            {"kind": "workflow", "tool_name": "spawn_agent", "arguments": {"message": "review A"}},
        ]
        self.assertIsNone(deterministic_required_action(actions))


if __name__ == "__main__":
    unittest.main()
