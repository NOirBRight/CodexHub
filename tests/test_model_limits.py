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
        self.assertEqual(limits[("ollama-cloud", "glm-5.2")].effective_context_window, 1000000)
        self.assertEqual(limits[("volc", "glm-5.2")].effective_context_window, 1024000)

    def test_unknown_effective_window_is_omitted_while_api_max_is_retained(self):
        limits = load_resolved_model_limits(ROOT / "config" / "resolved_model_limits.json")
        model = {"context_window": 200000}

        apply_resolved_model_limits(model, limits[("openai", "gpt-5.6-terra")])

        self.assertNotIn("context_window", model)
        self.assertEqual(model["max_context_window"], 1050000)
        self.assertEqual(model["confidence"], "max_only")

    def test_missing_optional_registry_is_safe_for_isolated_runtime_tests(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(load_resolved_model_limits(Path(root) / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
