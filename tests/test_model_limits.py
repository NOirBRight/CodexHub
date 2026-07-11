import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))

from model_limits import apply_resolved_model_limits, load_resolved_model_limits


class ResolvedModelLimitsTests(unittest.TestCase):
    def test_registry_is_provider_scoped_and_keeps_effective_separate_from_api_max(self):
        limits = load_resolved_model_limits(ROOT / "config" / "resolved_model_limits.json")

        sol = limits[("openai", "gpt-5.6-sol")]
        self.assertEqual(sol.effective_context_window, 353400)
        self.assertEqual(sol.max_context_window, 1050000)
        self.assertNotEqual(sol.effective_context_window, sol.max_context_window)
        terra = limits[("openai", "gpt-5.6-terra")]
        luna = limits[("openai", "gpt-5.6-luna")]
        self.assertEqual(terra.effective_context_window, 353400)
        self.assertEqual(luna.effective_context_window, 353400)
        self.assertEqual(terra.max_context_window, 1050000)
        self.assertEqual(luna.max_context_window, 1050000)
        self.assertEqual(terra.confidence, "verified")
        self.assertEqual(luna.confidence, "verified")
        self.assertEqual(limits[("ollama-cloud", "glm-5.2")].effective_context_window, 1000000)
        self.assertEqual(limits[("volc", "glm-5.2")].effective_context_window, 1024000)

    def test_verified_effective_window_replaces_stale_value_while_api_max_is_retained(self):
        limits = load_resolved_model_limits(ROOT / "config" / "resolved_model_limits.json")
        model = {"context_window": 200000}

        apply_resolved_model_limits(model, limits[("openai", "gpt-5.6-terra")])

        self.assertEqual(model["context_window"], 353400)
        self.assertEqual(model["max_context_window"], 1050000)
        self.assertEqual(model["confidence"], "verified")

    def test_missing_optional_registry_is_safe_for_isolated_runtime_tests(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(load_resolved_model_limits(Path(root) / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
