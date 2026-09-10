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

    def test_legacy_modes_remain_readable_but_never_enable_gateway_scheduling(self):
        context = {"repair_policy": REPAIR_CODEX_SUBAGENT}
        for value in ("strict", "guided", "assisted", "invalid"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": value}, clear=False
            ):
                self.assertIn(subagent_assist_mode(), {"strict", "guided", "assisted"})
                self.assertFalse(guidance_enabled(context))
                self.assertFalse(semantic_repair_enabled(context))

    def test_scheduler_cannot_select_a_client_action(self):
        action = {
            "kind": "protocol",
            "tool_name": "wait_agent",
            "arguments": {"targets": ["agent-1"]},
        }
        self.assertIsNone(deterministic_required_action([action]))

    def test_gateway_settings_uses_policy_helpers_as_single_source_of_truth(self):
        self.assertIs(gateway_settings._subagent_policy_assist_mode, subagent_assist_mode)
        self.assertIs(gateway_settings._subagent_policy_guidance_enabled, guidance_enabled)
        self.assertIs(gateway_settings._subagent_policy_semantic_repair_enabled, semantic_repair_enabled)


if __name__ == "__main__":
    unittest.main()
