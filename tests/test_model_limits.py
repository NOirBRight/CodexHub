import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))

from model_limits import (
    OFFICIAL_AUTO_COMPACT_TOKEN_LIMIT,
    OFFICIAL_CONTEXT_FALLBACK_WINDOW,
    CURRENT_DIRECT_OFFICIAL_SOURCE,
    apply_resolved_model_limits,
    load_resolved_model_limits,
    resolve_official_context_budget,
)


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

    def test_current_direct_context_budget_overrides_a_larger_probe_cache(self):
        budget = resolve_official_context_budget(
            direct_context_window=272_000,
            direct_max_context_window=1_050_000,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=353_400,
        )

        self.assertEqual(budget.context_window, 272_000)
        self.assertEqual(budget.max_context_window, 272_000)
        self.assertEqual(budget.effective_context_window_percent, 95)
        self.assertEqual(budget.effective_context_window, 258_400)
        self.assertEqual(budget.model_context_window, 272_000)
        self.assertEqual(budget.model_auto_compact_token_limit, 240_000)
        self.assertEqual(budget.source, "current_direct_official")
        self.assertEqual(budget.freshness, "fresh")
        self.assertLess(budget.model_auto_compact_token_limit, 249_433)

    def test_missing_stale_and_contradictory_direct_contexts_fail_safe(self):
        missing = resolve_official_context_budget(
            direct_context_window=None,
            direct_freshness="missing",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=353_400,
        )
        stale = resolve_official_context_budget(
            direct_context_window=353_400,
            direct_freshness="stale",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=353_400,
        )
        contradictory = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=200_000,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=353_400,
        )

        for budget in (missing, stale, contradictory):
            self.assertLessEqual(budget.context_window, OFFICIAL_CONTEXT_FALLBACK_WINDOW)
            self.assertLessEqual(
                budget.model_auto_compact_token_limit,
                OFFICIAL_AUTO_COMPACT_TOKEN_LIMIT,
            )
            self.assertEqual(budget.source, "conservative_fallback")

        self.assertEqual(contradictory.freshness, "contradictory")

        lower_stale = resolve_official_context_budget(
            direct_context_window=200_000,
            direct_freshness="stale",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=353_400,
        )
        self.assertEqual(lower_stale.context_window, 200_000)
        self.assertEqual(lower_stale.source, "conservative_fallback")

    def test_future_higher_context_requires_a_fresh_direct_signal(self):
        fresh = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=1_050_000,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=353_400,
        )
        stale = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=1_050_000,
            direct_freshness="stale",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=353_400,
        )

        self.assertEqual(fresh.context_window, 400_000)
        self.assertEqual(fresh.source, "current_direct_official")
        self.assertEqual(stale.context_window, OFFICIAL_CONTEXT_FALLBACK_WINDOW)
        self.assertEqual(stale.source, "conservative_fallback")

        untrusted = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=1_050_000,
            direct_freshness="fresh",
            direct_source="bundled_seed",
            fallback_context_window=353_400,
        )
        self.assertEqual(untrusted.context_window, OFFICIAL_CONTEXT_FALLBACK_WINDOW)
        self.assertEqual(untrusted.source, "conservative_fallback")


if __name__ == "__main__":
    unittest.main()
