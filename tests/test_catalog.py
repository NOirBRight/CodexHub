import json
from pathlib import Path
import tempfile
import unittest

from catalog import (
    CatalogPolicy,
    canonical_model_id,
    display_name_for,
    load_catalog_models,
    load_policy,
    should_include_model,
)


POLICY_PATH = Path(__file__).resolve().parents[1] / "catalog_policy.toml"


class CatalogPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = CatalogPolicy(
            denied_models={"glm-5.1", "glm-5.1:cloud", "qwen3-embedding", "qwen3-embedding:latest"},
            denied_substrings={"embedding"},
            display_names={
                "glm-5.2": "GLM-5.2",
                "minimax-m3": "MiniMax-M3",
                "kimi-k2.7-code": "Kimi K2.7 Code",
                "deepseek-v4-pro": "DeepSeek V4 Pro",
                "deepseek-v4-flash": "DeepSeek V4 Flash",
                "gemini-3-flash-preview": "Gemini 3 Flash Preview",
                "kimi-k2.6": "Kimi K2.6",
            },
            official_models={"gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"},
            allowed_ollama_cloud_models={
                "minimax-m3",
                "glm-5.2",
                "kimi-k2.7-code",
                "gemini-3-flash-preview",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
            },
            allowed_provider_models={
                "volc/glm-5.2",
                "volc/minimax-m3",
                "minimax-cn/minimax-m3",
            },
        )

    def test_cloud_suffix_is_removed(self):
        self.assertEqual(canonical_model_id("kimi-k2.7-code:cloud"), "kimi-k2.7-code")

    def test_glm_5_1_is_not_visible(self):
        self.assertFalse(should_include_model("glm-5.1", self.policy))
        self.assertFalse(should_include_model("glm-5.1:cloud", self.policy))

    def test_real_policy_denies_glm_5_1_tagged_variants(self):
        policy = load_policy(POLICY_PATH)

        self.assertFalse(should_include_model("glm-5.1:latest", policy))
        self.assertFalse(should_include_model("glm-5.1:cloud", policy))
        self.assertTrue(should_include_model("glm-5.2:cloud", policy))
        self.assertTrue(should_include_model("minimax-m3", policy))
        self.assertTrue(should_include_model("volc/glm-5.2", policy))
        self.assertTrue(should_include_model("minimax-cn/minimax-m3", policy))
        self.assertFalse(should_include_model("volc/minimax-m2.7", policy))
        self.assertFalse(should_include_model("gemma3:12b", policy))
        self.assertTrue(should_include_model("gpt-5.5", policy))

    def test_provider_namespace_tags_are_not_base_matched(self):
        policy = CatalogPolicy(
            denied_models={"provider/model"},
            denied_substrings=set(),
            display_names={},
            auto_include_ollama_cloud=True,
        )

        self.assertTrue(should_include_model("provider/model:latest", policy))

    def test_embedding_models_are_not_visible(self):
        self.assertFalse(should_include_model("qwen3-embedding:latest", self.policy))

    def test_display_name_override(self):
        self.assertEqual(display_name_for("kimi-k2.7-code", self.policy), "Kimi K2.7 Code")

    def test_load_catalog_models_reads_models_array(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "catalog.json"
            catalog_path.write_text(json.dumps({"models": [{"slug": "x"}]}), encoding="utf-8")

            self.assertEqual(load_catalog_models(catalog_path), [{"slug": "x"}])


if __name__ == "__main__":
    unittest.main()
