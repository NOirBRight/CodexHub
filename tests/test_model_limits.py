import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))

from model_limits import (
    CURRENT_DIRECT_OFFICIAL_SOURCE,
    FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
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
            direct_effective_context_window_percent=95,
            direct_auto_compact_token_limit=240_000,
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

    def test_direct_snapshot_uses_its_dynamic_effective_and_compaction_fields(self):
        budget = resolve_official_context_budget(
            direct_context_window=300_000,
            direct_max_context_window=900_000,
            direct_effective_context_window_percent=80,
            direct_auto_compact_token_limit=210_000,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
        )

        self.assertEqual(budget.context_window, 300_000)
        self.assertEqual(budget.max_context_window, 300_000)
        self.assertEqual(budget.effective_context_window_percent, 80)
        self.assertEqual(budget.effective_context_window, 240_000)
        self.assertEqual(budget.model_auto_compact_token_limit, 210_000)

    def test_missing_direct_compact_threshold_uses_native_ninety_percent_bound(self):
        budget = resolve_official_context_budget(
            direct_context_window=272_000,
            direct_effective_context_window_percent=95,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
        )

        self.assertEqual(budget.effective_context_window, 258_400)
        self.assertEqual(budget.model_auto_compact_token_limit, 244_800)
        self.assertLess(budget.model_auto_compact_token_limit, 249_433)

    def test_fresh_direct_cache_authority_can_tighten_or_adopt_a_higher_budget(self):
        lower = resolve_official_context_budget(
            direct_context_window=272_000,
            direct_max_context_window=272_000,
            direct_effective_context_window_percent=95,
            direct_freshness="fresh",
            direct_source=FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
            fallback_context_window=353_400,
            fallback_effective_context_window_percent=100,
        )
        higher = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=400_000,
            direct_effective_context_window_percent=100,
            direct_freshness="fresh",
            direct_source=FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
            fallback_context_window=272_000,
            fallback_effective_context_window_percent=95,
        )
        stale_higher = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=400_000,
            direct_effective_context_window_percent=100,
            direct_freshness="stale",
            direct_source=FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
            fallback_context_window=272_000,
            fallback_effective_context_window_percent=95,
        )

        self.assertEqual(lower.source, FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE)
        self.assertEqual(lower.context_window, 272_000)
        self.assertEqual(lower.effective_context_window, 258_400)
        self.assertEqual(lower.model_auto_compact_token_limit, 244_800)
        self.assertLess(lower.model_auto_compact_token_limit, 249_433)
        self.assertEqual(higher.source, FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE)
        self.assertEqual(higher.context_window, 400_000)
        self.assertEqual(stale_higher.source, "degraded_last_known_official")
        self.assertEqual(stale_higher.context_window, 272_000)

    def test_missing_direct_effective_percent_cannot_expand_a_budget(self):
        fallback = {
            "fallback_context_window": 272_000,
            "fallback_effective_context_window_percent": 95,
            "fallback_auto_compact_token_limit": 240_000,
        }

        missing_without_prior = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
        )
        missing_with_prior = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            **fallback,
        )

        self.assertIsNone(missing_without_prior)
        self.assertEqual(missing_with_prior.context_window, 272_000)
        self.assertEqual(missing_with_prior.effective_context_window_percent, 95)
        self.assertEqual(missing_with_prior.source, "degraded_last_known_official")
        self.assertEqual(missing_with_prior.freshness, "missing")

    def test_missing_stale_and_contradictory_direct_contexts_fail_safe(self):
        self.assertIsNone(
            resolve_official_context_budget(
                direct_context_window=None,
                direct_freshness="missing",
                direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            )
        )
        fallback = {
            "fallback_context_window": 272_000,
            "fallback_effective_context_window_percent": 95,
            "fallback_auto_compact_token_limit": 240_000,
        }
        missing = resolve_official_context_budget(
            direct_context_window=None,
            direct_freshness="missing",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            **fallback,
        )
        stale = resolve_official_context_budget(
            direct_context_window=353_400,
            direct_freshness="stale",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            **fallback,
        )
        contradictory = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=200_000,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            **fallback,
        )

        for budget in (missing, stale, contradictory):
            self.assertLessEqual(budget.context_window, 272_000)
            self.assertLessEqual(budget.model_auto_compact_token_limit, 240_000)
            self.assertEqual(budget.source, "degraded_last_known_official")

        self.assertEqual(contradictory.freshness, "contradictory")

        lower_stale = resolve_official_context_budget(
            direct_context_window=200_000,
            direct_freshness="stale",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            **fallback,
        )
        self.assertEqual(lower_stale.context_window, 200_000)
        self.assertEqual(lower_stale.source, "degraded_last_known_official")

    def test_future_higher_context_requires_a_fresh_direct_signal(self):
        fresh = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=1_050_000,
            direct_effective_context_window_percent=100,
            direct_freshness="fresh",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=272_000,
            fallback_effective_context_window_percent=95,
        )
        stale = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=1_050_000,
            direct_effective_context_window_percent=100,
            direct_freshness="stale",
            direct_source=CURRENT_DIRECT_OFFICIAL_SOURCE,
            fallback_context_window=272_000,
            fallback_effective_context_window_percent=95,
        )

        self.assertEqual(fresh.context_window, 400_000)
        self.assertEqual(fresh.source, "current_direct_official")
        self.assertEqual(stale.context_window, 272_000)
        self.assertEqual(stale.source, "degraded_last_known_official")

        untrusted = resolve_official_context_budget(
            direct_context_window=400_000,
            direct_max_context_window=1_050_000,
            direct_effective_context_window_percent=100,
            direct_freshness="fresh",
            direct_source="bundled_seed",
            fallback_context_window=272_000,
            fallback_effective_context_window_percent=95,
        )
        self.assertEqual(untrusted.context_window, 272_000)
        self.assertEqual(untrusted.source, "degraded_last_known_official")


if __name__ == "__main__":
    unittest.main()
