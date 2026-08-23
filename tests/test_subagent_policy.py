import os
import unittest
from unittest.mock import patch

import gateway_settings
from subagent_policy import (
    REPAIR_CODEX_SUBAGENT,
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
        context = {"repair_policy": REPAIR_CODEX_SUBAGENT}
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "guided"}, clear=False):
            self.assertTrue(guidance_enabled(context))
            self.assertFalse(semantic_repair_enabled(context))

    def test_guidance_and_repair_require_repair_policy(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            self.assertFalse(guidance_enabled({}))
            self.assertFalse(guidance_enabled({"repair_policy": "none"}))
            self.assertTrue(guidance_enabled({"repair_policy": REPAIR_CODEX_SUBAGENT}))
            self.assertFalse(semantic_repair_enabled({}))
            self.assertFalse(semantic_repair_enabled({"repair_policy": "none"}))
            self.assertTrue(semantic_repair_enabled({"repair_policy": REPAIR_CODEX_SUBAGENT}))

    def test_raw_probe_disables_guidance_and_repair(self):
        context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "raw_provider_probe": True}
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            self.assertFalse(guidance_enabled(context))
            self.assertFalse(semantic_repair_enabled(context))

    def test_deterministic_required_action_returns_single_known_action(self):
        action = {"kind": "protocol", "tool_name": "wait_agent", "arguments": {"targets": ["agent-1"]}}
        self.assertEqual(deterministic_required_action([action]), action)

    def test_deterministic_required_action_refuses_multiple_valid_actions(self):
        actions = [
            {"kind": "workflow", "tool_name": "spawn_agent", "arguments": {"message": "task B"}},
            {"kind": "workflow", "tool_name": "spawn_agent", "arguments": {"message": "review A"}},
        ]
        self.assertIsNone(deterministic_required_action(actions))

    def test_gateway_settings_uses_policy_helpers_as_single_source_of_truth(self):
        self.assertIs(gateway_settings._subagent_policy_assist_mode, subagent_assist_mode)
        self.assertIs(gateway_settings._subagent_policy_guidance_enabled, guidance_enabled)
        self.assertIs(gateway_settings._subagent_policy_semantic_repair_enabled, semantic_repair_enabled)


if __name__ == "__main__":
    unittest.main()
