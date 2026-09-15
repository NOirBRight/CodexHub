import json
from pathlib import Path
import tempfile
import unittest

from catalog import (
    CatalogPolicy,
    canonical_model_id,
    catalog_or_wire_display_name,
    catalog_owned_display_name,
    compose_flat_label,
    display_name_for,
    load_catalog_models,
    load_policy,
    should_include_external_provider_model,
    should_include_model,
)


POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "catalog_policy.toml"


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

    def test_load_policy_preserves_user_disabled_retired_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "catalog_policy.toml"
            policy_path.write_text(
                "[visibility]\ndenied_models = [\"glm-5.1\"]\n",
                encoding="utf-8",
            )

            self.assertIn("glm-5.1", load_policy(policy_path).denied_models)

    def test_real_policy_allows_current_official_cloud_models(self):
        policy = load_policy(POLICY_PATH)

        self.assertTrue(should_include_model("glm-5.1", policy))
        self.assertTrue(should_include_model("glm-5.1:cloud", policy))
        self.assertTrue(should_include_model("glm-5.2:cloud", policy))
        self.assertTrue(should_include_model("glm-5.3:cloud", policy))
        self.assertTrue(should_include_model("minimax-m3", policy))
        self.assertTrue(should_include_model("volc/glm-5.3", policy))
        self.assertTrue(should_include_model("minimax-cn/MiniMax-M3", policy))
        self.assertTrue(should_include_model("minimax-cn/minimax-m3", policy))
        self.assertTrue(should_include_model("volc/minimax-m3", policy))
        self.assertTrue(should_include_model("kimi/kimi-k3", policy))
        self.assertFalse(should_include_model("volc/minimax-m2.7", policy))
        self.assertFalse(should_include_model("gemma3:12b", policy))
        self.assertTrue(should_include_model("gpt-5.5", policy))
        self.assertIn("gpt-6-astra", policy.official_models)
        self.assertIn("gpt-5.6-sol", policy.official_models)

    def test_external_provider_models_use_runtime_config_visibility_plus_policy_denies(self):
        policy = load_policy(POLICY_PATH)

        self.assertTrue(should_include_external_provider_model("volc/minimax-m3", policy))
        self.assertFalse(should_include_external_provider_model("volc/qwen3-embedding", policy))
        self.assertTrue(should_include_external_provider_model("ollama-cloud/glm-5.1", policy))

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

    def test_flat_label_composes_prefix_unless_name_already_starts_with_it(self):
        self.assertEqual(compose_flat_label("Ollama", "GLM-5.3"), "Ollama GLM-5.3")
        self.assertEqual(compose_flat_label("Volc", "GLM-5.3"), "Volc GLM-5.3")
        self.assertEqual(compose_flat_label("Kimi CN", "K3"), "Kimi CN K3")
        self.assertEqual(compose_flat_label("Kimi", "K3"), "Kimi K3")
        self.assertEqual(compose_flat_label("MiniMax.cn", "MiniMax M3"), "MiniMax.cn MiniMax M3")
        self.assertEqual(compose_flat_label("Ollama", "Ollama GLM-5.3"), "Ollama GLM-5.3")
        self.assertEqual(compose_flat_label("", "GLM-5.3"), "GLM-5.3")
        self.assertEqual(compose_flat_label(None, "GLM-5.3"), "GLM-5.3")

    def test_catalog_owned_display_name_rewrites_prefixed_catalog_string_only(self):
        self.assertEqual(
            catalog_owned_display_name("Ollama GLM-5.3", "Ollama", "GLM-5.3"),
            "GLM-5.3",
        )
        self.assertEqual(
            catalog_owned_display_name("GLM-5.3", "Ollama", "GLM-5.3"),
            "GLM-5.3",
        )
        self.assertEqual(
            catalog_owned_display_name("My GLM", "Ollama", "GLM-5.3"),
            "My GLM",
        )
        self.assertEqual(
            catalog_owned_display_name("MiniMax M3", "MiniMax.cn", "MiniMax M3"),
            "MiniMax M3",
        )
        self.assertIsNone(catalog_owned_display_name(None, "Ollama", "GLM-5.3"))

    def test_catalog_or_wire_display_name_prefers_stored_then_catalog_then_wire_id(self):
        self.assertEqual(
            catalog_or_wire_display_name("My GLM", self.policy, "glm-5.2"),
            "My GLM",
        )
        self.assertEqual(
            catalog_or_wire_display_name(None, self.policy, "glm-5.2"),
            "GLM-5.2",
        )
        self.assertEqual(
            catalog_or_wire_display_name(None, self.policy, "volc/my-model", "my-model"),
            "my-model",
        )
        self.assertEqual(
            catalog_or_wire_display_name(
                "deepseek/deepseek-v4.1-flash",
                self.policy,
                "commandcode/deepseek/deepseek-v4.1-flash",
                "deepseek/deepseek-v4.1-flash",
            ),
            "deepseek-v4.1-flash",
        )
        self.assertEqual(
            catalog_or_wire_display_name(
                None,
                self.policy,
                "commandcode/deepseek/deepseek-v4.1-flash",
            ),
            "deepseek-v4.1-flash",
        )

    def test_load_catalog_models_reads_models_array(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "catalog.json"
            catalog_path.write_text(json.dumps({"models": [{"slug": "x"}]}), encoding="utf-8")

            self.assertEqual(load_catalog_models(catalog_path), [{"slug": "x"}])


if __name__ == "__main__":
    unittest.main()
