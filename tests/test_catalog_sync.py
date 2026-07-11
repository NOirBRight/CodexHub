import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from catalog import CatalogPolicy
import catalog_sync
from catalog_sync import build_codex_catalog, diff_model_state, discover_ollama_ids
from providers_config import ModelConfig, ProviderConfig


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.policy = CatalogPolicy(
            denied_models={"glm-5.1"},
            denied_substrings={"embedding"},
            display_names={
                "gpt-5.5": "GPT-5.5",
                "gpt-5.4": "GPT-5.4",
                "gpt-5.4-mini": "GPT-5.4-Mini",
                "gpt-5.3-codex-spark": "GPT-5.3-Codex-Spark",
                "glm-5.2": "GLM-5.2",
                "kimi-k2.7-code": "Kimi K2.7 Code",
            },
            official_models=("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"),
            allowed_ollama_cloud_models=(
                "minimax-m3",
                "glm-5.2",
                "kimi-k2.7-code",
                "gemini-3-flash-preview",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
            ),
            allowed_provider_models=(
                "volc/ark-code-latest",
                "volc/glm-5.2",
                "volc/minimax-m3",
            ),
        )

    def test_build_catalog_keeps_official_and_excludes_glm_5_1(self):
        official = [{"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"}]
        ollama_ids = ["glm-5.2:cloud", "glm-5.1:cloud", "qwen3-embedding:latest", "kimi-k2.7-code:cloud"]
        catalog = build_codex_catalog(official, ollama_ids, self.policy, "0.142.0")
        slugs = [model["slug"] for model in catalog["models"]]
        self.assertEqual(
            slugs,
            [
                "gpt-5.5",
                "glm-5.2",
                "kimi-k2.7-code",
            ],
        )
        self.assertNotIn("gpt-5.4", slugs)
        self.assertNotIn("glm-5.1", slugs)

    def test_build_catalog_keeps_only_official_and_allowed_cloud_models(self):
        official = [
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
            {"slug": "gpt-5.4", "display_name": "GPT-5.4", "visibility": "list"},
        ]
        ollama_ids = [
            "gemma3:12b",
            "minimax-m3",
            "glm-5.2:cloud",
            "qwen3-embedding:latest",
            "gpt-oss:20b",
            "deepseek-v4-flash",
            "kimi-k2.7-code:cloud",
            "deepseek-v4-pro",
            "gemini-3-flash-preview",
        ]

        catalog = build_codex_catalog(official, ollama_ids, self.policy, "0.142.0")
        slugs = [model["slug"] for model in catalog["models"]]

        self.assertEqual(
            slugs,
            [
                "gpt-5.5",
                "gpt-5.4",
                "minimax-m3",
                "glm-5.2",
                "kimi-k2.7-code",
                "gemini-3-flash-preview",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
            ],
        )
        self.assertNotIn("gemma3:12b", slugs)
        self.assertNotIn("gpt-oss:20b", slugs)
        self.assertEqual(
            [model["priority"] for model in catalog["models"][2:]],
            [100, 101, 102, 103, 104, 105],
        )
        by_slug = {model["slug"]: model for model in catalog["models"]}
        self.assertEqual(by_slug["minimax-m3"]["context_window"], 524288)
        self.assertEqual(by_slug["minimax-m3"]["max_output_tokens"], 524288)
        self.assertEqual(by_slug["glm-5.2"]["context_window"], 1000000)
        self.assertEqual(by_slug["glm-5.2"]["max_output_tokens"], 131072)
        self.assertEqual(by_slug["gemini-3-flash-preview"]["context_window"], 1048576)
        self.assertEqual(by_slug["gemini-3-flash-preview"]["max_output_tokens"], 65536)
        self.assertEqual(by_slug["deepseek-v4-pro"]["context_window"], 524288)
        self.assertEqual(by_slug["deepseek-v4-pro"]["max_output_tokens"], 393216)
        self.assertEqual(by_slug["deepseek-v4-flash"]["context_window"], 1048576)
        self.assertEqual(by_slug["deepseek-v4-flash"]["max_output_tokens"], 393216)

    def test_build_catalog_runtime_ollama_models_use_provider_settings_instead_of_static_allowlist(self):
        policy = CatalogPolicy(
            denied_models={"blocked-model", "ollama-cloud/provider-blocked"},
            denied_substrings={"embedding"},
            display_names={},
            official_models=(),
            allowed_ollama_cloud_models=("glm-5.2",),
        )

        metadata = catalog_sync.ollama_provider_model_metadata(
            [
                {
                    "upstream_model": "runtime-model",
                    "context_window": 123000,
                    "max_output_tokens": 456,
                    "input_modalities": ("text", "image"),
                }
            ]
        )
        catalog = build_codex_catalog(
            [],
            ["runtime-model", "blocked-model", "provider-blocked"],
            policy,
            "0.142.0",
            ollama_model_metadata=metadata,
            use_ollama_policy_allowlist=False,
        )
        slugs = [model["slug"] for model in catalog["models"]]

        self.assertEqual(slugs, ["runtime-model"])
        model = catalog["models"][0]
        self.assertEqual(model["context_window"], 123000)
        self.assertEqual(model["max_output_tokens"], 456)
        self.assertEqual(model["input_modalities"], ["text", "image"])
        self.assertEqual(model["codex_proxy_metadata"]["context_source"], "providers_toml")
        self.assertEqual(model["codex_proxy_metadata"]["max_output_source"], "providers_toml")

    def test_build_catalog_applies_official_model_sort_order(self):
        official = [
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
            {"slug": "gpt-5.4", "display_name": "GPT-5.4", "visibility": "list"},
        ]

        catalog = build_codex_catalog(
            official,
            [],
            self.policy,
            "0.142.0",
            official_model_sort_order=["openai/gpt-5.4-mini", "openai/gpt-5.5"],
        )

        self.assertEqual(
            [model["slug"] for model in catalog["models"]],
            [
                "gpt-5.5",
                "gpt-5.4",
            ],
        )

    def test_build_catalog_filters_disabled_official_models(self):
        official = [
            {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
            {"slug": "gpt-5.4", "display_name": "GPT-5.4", "visibility": "list"},
        ]

        catalog = build_codex_catalog(
            official,
            [],
            self.policy,
            "0.142.0",
            disabled_official_model_ids=["openai/gpt-5.4"],
        )

        self.assertEqual(
            [model["slug"] for model in catalog["models"]],
            [
                "gpt-5.5",
            ],
        )

    def test_official_fast_metadata_is_preserved(self):
        official = [
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "visibility": "list",
                "additional_speed_tiers": ["fast"],
                "service_tiers": [{"id": "priority", "name": "Fast"}],
            },
            {
                "slug": "gpt-5.4",
                "display_name": "GPT-5.4",
                "visibility": "list",
                "additional_speed_tiers": ["fast"],
                "service_tiers": [{"id": "priority", "name": "Fast"}],
            },
            {
                "slug": "gpt-5.4-mini",
                "display_name": "GPT-5.4-Mini",
                "visibility": "list",
            },
        ]

        catalog = build_codex_catalog(official, [], self.policy, "0.142.0")
        by_slug = {model["slug"]: model for model in catalog["models"]}

        self.assertEqual(by_slug["gpt-5.5"]["display_name"], "5.5")
        self.assertEqual(by_slug["gpt-5.5"]["additional_speed_tiers"], ["fast"])
        self.assertEqual(by_slug["gpt-5.5"]["codex_proxy_metadata"]["upstream_model"], "gpt-5.5")
        self.assertEqual(by_slug["gpt-5.4"]["service_tiers"][0]["id"], "priority")
        self.assertNotIn("context_window", by_slug["gpt-5.4-mini"])
        self.assertEqual(by_slug["gpt-5.4-mini"]["additional_speed_tiers"], [])
        self.assertEqual(by_slug["gpt-5.4-mini"]["service_tiers"], [])

    def test_shared_model_identity_vectors_reject_only_unknown_official_aliases(self):
        fixture_path = Path(__file__).parent / "fixtures" / "model_identity_vectors.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        for vector in fixture["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(
                    catalog_sync.normalize_official_model_id(vector["input"]),
                    vector["expected"],
                )

    def test_bundled_seed_does_not_authorize_stale_official_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_seed = root / "runtime.json"
            bundled_seed = root / "bundled.json"
            runtime_seed.write_text(
                json.dumps({"models": [{"slug": "gpt-5.6-current"}]}),
                encoding="utf-8",
            )
            bundled_seed.write_text(
                json.dumps({"models": [{"slug": "gpt-5.6-stale"}]}),
                encoding="utf-8",
            )
            with (
                patch("catalog_sync.load_policy", return_value=self.policy),
                patch("catalog_sync.RUNTIME_OFFICIAL_SEED_PATH", runtime_seed),
                patch(
                    "catalog_sync.official_seed_catalog_paths",
                    return_value=[runtime_seed, bundled_seed],
                ),
            ):
                known = catalog_sync.known_official_model_ids()

        self.assertIn("gpt-5.6-current", known)
        self.assertNotIn("gpt-5.6-stale", known)

    def test_official_catalog_preserves_app_cli_metadata_without_generic_defaults(self):
        official = [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "context_window": 400000,
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Light"},
                    {"effort": "medium", "description": "Medium"},
                    {"effort": "high", "description": "High"},
                    {"effort": "xhigh", "description": "Extra High"},
                    {"effort": "max", "description": "Max"},
                    {"effort": "ultra", "description": "Ultra"},
                ],
                "multi_agent_version": "v2",
                "tool_mode": "native",
                "model_messages": {"upgrade": "Use Sol"},
                "skills_instructions": "Official skills contract",
                "web_search_tool_type": "text",
                "use_responses_lite": True,
                "availability": {"plan": "plus"},
                "upgrade": "gpt-5.7-sol",
                "upgrade_info": {"message": "Upgrade available"},
                "comp_hash": "sol-compat-hash",
            }
        ]

        catalog = build_codex_catalog(official, [], self.policy, "0.144.0")
        model = catalog["models"][0]

        self.assertEqual(model["slug"], "gpt-5.6-sol")
        self.assertEqual(model["display_name"], "5.6 Sol")
        for key, value in official[0].items():
            if key not in {"slug", "display_name"}:
                self.assertEqual(model[key], value, key)
        self.assertEqual(model["shell_type"], "shell_command")
        self.assertEqual(model["supports_parallel_tool_calls"], True)
        self.assertEqual(model["default_reasoning_level"], "medium")
        self.assertIn("base_instructions", model)
        self.assertEqual(model["supported_in_api"], True)
        self.assertEqual(
            model["codex_proxy_metadata"],
            {
                "provider": "openai",
                "upstream_name": "official",
                "upstream_model": "gpt-5.6-sol",
            },
        )

    def test_official_alias_duplicates_collapse_to_fresh_bare_record(self):
        official = [
            {
                "slug": "openai/gpt-5.6-sol",
                "display_name": "Legacy Sol",
                "context_window": 1,
                "enabled": True,
            },
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "context_window": 400000,
                "enabled": False,
                "multi_agent_version": "v2",
            },
        ]

        catalog = build_codex_catalog(official, [], self.policy, "0.144.0")
        models = catalog["models"]

        self.assertEqual([model["slug"] for model in models], ["gpt-5.6-sol"])
        self.assertEqual(models[0]["display_name"], "5.6 Sol")
        self.assertEqual(models[0]["context_window"], 400000)
        self.assertEqual(models[0]["multi_agent_version"], "v2")
        self.assertTrue(models[0]["enabled"])

    def test_load_official_seed_models_falls_back_to_runtime_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled_seed = root / "missing" / "openai-plus-ollama-cloud.json"
            runtime_seed = root / "runtime" / "openai-plus-ollama-cloud.json"
            runtime_seed.parent.mkdir(parents=True)
            runtime_seed.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.5",
                                "display_name": "GPT-5.5",
                                "context_window": 272000,
                                "max_context_window": 272000,
                                "additional_speed_tiers": ["fast"],
                                "service_tiers": [{"id": "priority", "name": "Fast"}],
                            },
                            {"slug": "not-gpt", "display_name": "Not GPT"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            models = catalog_sync.load_official_seed_models(bundled_seed, runtime_path=runtime_seed)

        self.assertEqual([model["slug"] for model in models], ["gpt-5.5"])
        self.assertEqual(models[0]["context_window"], 272000)
        self.assertEqual(models[0]["additional_speed_tiers"], ["fast"])

    def test_load_official_seed_models_prefers_runtime_subscription_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled_seed = root / "bundled" / "openai-plus-ollama-cloud.json"
            runtime_seed = root / "runtime" / "openai-plus-ollama-cloud.json"
            bundled_seed.parent.mkdir(parents=True)
            runtime_seed.parent.mkdir(parents=True)
            bundled_seed.write_text(
                json.dumps({"models": [{"slug": "gpt-5.5", "display_name": "GPT-5.5"}]}),
                encoding="utf-8",
            )
            runtime_seed.write_text(
                json.dumps({"models": [{"slug": "gpt-5.6", "display_name": "GPT-5.6"}]}),
                encoding="utf-8",
            )

            models = catalog_sync.load_official_seed_models(bundled_seed, runtime_path=runtime_seed)

        self.assertEqual([model["slug"] for model in models], ["gpt-5.6"])

    def test_build_catalog_uses_subscription_official_models_before_policy_allowlist(self):
        official = [{"slug": "gpt-5.6", "display_name": "GPT-5.6", "visibility": "list"}]

        catalog = build_codex_catalog(official, [], self.policy, "0.142.0")

        self.assertEqual([model["slug"] for model in catalog["models"]], ["gpt-5.6"])
        self.assertEqual(catalog["models"][0]["display_name"], "5.6")

    def test_build_catalog_exposes_official_models_without_provider_prefix(self):
        official = [{"slug": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "visibility": "list"}]
        external_models = [
            {
                "alias": "volc/glm-5.2",
                "provider_alias": "volc",
                "upstream_name": "volc",
                "upstream_model": "glm-5.2",
            }
        ]

        catalog = build_codex_catalog(
            official,
            [],
            self.policy,
            "0.142.0",
            external_models=external_models,
        )
        by_slug = {model["slug"]: model for model in catalog["models"]}

        self.assertEqual(list(by_slug), ["gpt-5.6-sol", "volc/glm-5.2"])
        self.assertNotIn("openai/gpt-5.6-sol", by_slug)
        self.assertEqual(
            by_slug["gpt-5.6-sol"]["codex_proxy_metadata"],
            {
                "provider": "openai",
                "upstream_name": "official",
                "upstream_model": "gpt-5.6-sol",
            },
        )
        self.assertEqual(by_slug["volc/glm-5.2"]["codex_proxy_metadata"]["provider"], "volc")

    def test_minimal_official_models_use_codex_defaults(self):
        policy = CatalogPolicy(
            denied_models=set(),
            denied_substrings=set(),
            display_names={
                "gpt-5.5": "GPT-5.5",
                "gpt-5.5-fast": "GPT-5.5 Fast",
                "gpt-5.4": "GPT-5.4",
                "gpt-5.4-fast": "GPT-5.4 Fast",
                "gpt-5.4-mini": "GPT-5.4-Mini",
                "gpt-5.3-codex-spark": "GPT-5.3-Codex-Spark",
            },
            official_models=(
                "gpt-5.5",
                "gpt-5.5-fast",
                "gpt-5.4",
                "gpt-5.4-fast",
                "gpt-5.4-mini",
                "gpt-5.3-codex-spark",
            ),
        )

        catalog = build_codex_catalog([], [], policy, "0.142.0")
        by_slug = {model["slug"]: model for model in catalog["models"]}

        self.assertEqual(by_slug["gpt-5.5"]["context_window"], 258400)
        self.assertEqual(by_slug["gpt-5.5"]["max_context_window"], 258400)
        self.assertEqual(catalog_sync.OFFICIAL_MODEL_DEFAULTS["gpt-5.5-fast"]["context_window"], 258400)
        self.assertEqual(catalog_sync.OFFICIAL_MODEL_DEFAULTS["gpt-5.5-fast"]["max_context_window"], 258400)
        self.assertEqual(by_slug["gpt-5.5"]["additional_speed_tiers"], ["fast"])
        self.assertEqual(by_slug["gpt-5.5"]["service_tiers"][0]["id"], "priority")
        self.assertEqual(by_slug["gpt-5.5"]["default_reasoning_level"], "medium")
        self.assertNotIn("gpt-5.5-fast", by_slug)
        self.assertEqual(by_slug["gpt-5.4"]["context_window"], 272000)
        self.assertEqual(by_slug["gpt-5.4"]["additional_speed_tiers"], ["fast"])
        self.assertNotIn("gpt-5.4-fast", by_slug)
        self.assertEqual(by_slug["gpt-5.4-mini"]["context_window"], 272000)
        self.assertEqual(by_slug["gpt-5.4-mini"]["additional_speed_tiers"], [])
        self.assertEqual(by_slug["gpt-5.3-codex-spark"]["context_window"], 128000)
        for model_id in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"):
            self.assertEqual(
                [entry["effort"] for entry in by_slug[model_id]["supported_reasoning_levels"]],
                ["low", "medium", "high", "xhigh", "max"],
            )
            for required_key in (
                "shell_type",
                "priority",
                "base_instructions",
                "model_messages",
                "include_skills_usage_instructions",
                "truncation_policy",
                "input_modalities",
                "supports_parallel_tool_calls",
            ):
                self.assertIn(required_key, by_slug[model_id])

    def test_build_catalog_preserves_fallback_metadata_for_ollama_models(self):
        fallback_models = [
            {
                "slug": "glm-5.2",
                "display_name": "Fallback GLM",
                "description": "Fallback description",
                "context_window": 128000,
                "visibility": "list",
                "priority": 42,
            }
        ]

        catalog = build_codex_catalog([], ["glm-5.2:cloud"], self.policy, "0.142.0", fallback_models=fallback_models)
        glm_model = next(model for model in catalog["models"] if model["slug"] == "glm-5.2")

        self.assertEqual(glm_model["display_name"], "GLM-5.2")
        self.assertEqual(glm_model["description"], "Fallback description")
        self.assertEqual(glm_model["context_window"], 1000000)
        self.assertEqual(glm_model["max_context_window"], 1000000)
        self.assertEqual(glm_model["max_output_tokens"], 131072)

    def test_build_catalog_appends_provider_prefixed_external_models(self):
        external_models = [
            {
                "alias": "volc/glm-5.2",
                "provider_alias": "volc",
                "upstream_name": "volcengine",
                "display_prefix": "Volc",
                "base_url": "https://ark.example.test/v1",
                "api_key": "secret-test-key",
                "upstream_model": "glm-5.2",
                "upstream_format": "chat_completions",
                "tool_protocol": "responses_structured",
                "priority_base": 200,
                "context_window": 1024000,
                "max_output_tokens": 4096,
                "input_modalities": ("text", "image"),
                "supported_reasoning_levels": ("high", "ultra", "low", "turbo", "max", "xhigh"),
                "default_reasoning_level": "high",
                "context_source": "providers_toml",
                "max_output_source": "providers_toml",
            },
            {
                "alias": "volc/minimax-m3",
                "provider_alias": "volc",
                "upstream_name": "volcengine",
                "display_prefix": "Volc",
                "base_url": "https://ark.example.test/v1",
                "api_key": "secret-test-key",
                "upstream_model": "minimax-m3",
                "priority_base": 200,
                "context_window": 512000,
                "max_output_tokens": 4096,
                "input_modalities": ("text", "image"),
                "context_source": "providers_toml",
                "max_output_source": "providers_toml",
            },
        ]

        catalog = build_codex_catalog([], [], self.policy, "0.142.0", external_models=external_models)
        slugs = [model["slug"] for model in catalog["models"]]

        self.assertEqual(slugs[-2:], ["volc/glm-5.2", "volc/minimax-m3"])
        by_slug = {model["slug"]: model for model in catalog["models"]}
        self.assertEqual(by_slug["volc/glm-5.2"]["display_name"], "Volc GLM 5.2")
        self.assertEqual(by_slug["volc/glm-5.2"]["context_window"], 1024000)
        self.assertEqual(by_slug["volc/glm-5.2"]["max_output_tokens"], 4096)
        self.assertEqual(by_slug["volc/glm-5.2"]["priority"], 200)
        self.assertEqual(by_slug["volc/glm-5.2"]["input_modalities"], ["text", "image"])
        self.assertEqual(by_slug["volc/glm-5.2"]["default_reasoning_level"], "high")
        self.assertEqual(
            [item["effort"] for item in by_slug["volc/glm-5.2"]["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(by_slug["volc/minimax-m3"]["priority"], 201)
        self.assertEqual(by_slug["volc/minimax-m3"]["input_modalities"], ["text", "image"])
        self.assertEqual(by_slug["volc/glm-5.2"]["codex_proxy_metadata"]["provider"], "volc")
        self.assertEqual(by_slug["volc/glm-5.2"]["codex_proxy_metadata"]["upstream_model"], "glm-5.2")
        self.assertEqual(
            by_slug["volc/glm-5.2"]["codex_proxy_metadata"]["upstream_format"],
            "chat_completions",
        )
        self.assertEqual(
            by_slug["volc/glm-5.2"]["codex_proxy_metadata"]["tool_protocol"],
            "responses_structured",
        )
        self.assertEqual(
            by_slug["volc/glm-5.2"]["description"],
            "External Volc model via providers.toml.",
        )
        self.assertNotIn("secret-test-key", json.dumps(catalog))

    def test_build_catalog_omits_empty_external_provider_source_metadata(self):
        external_models = [
            {
                "alias": "volc/glm-5.2",
                "provider_alias": "volc",
                "upstream_name": "volcengine",
                "display_prefix": "Volc",
                "base_url": "https://ark.example.test/v1",
                "api_key": "secret-test-key",
                "upstream_model": "glm-5.2",
                "priority_base": 200,
                "context_window": 1024000,
                "max_output_tokens": 4096,
                "input_modalities": ("text",),
                "context_source": None,
                "max_output_source": None,
            }
        ]

        catalog = build_codex_catalog([], [], self.policy, "0.142.0", external_models=external_models)
        model = next(model for model in catalog["models"] if model["slug"] == "volc/glm-5.2")
        metadata = model["codex_proxy_metadata"]

        self.assertEqual(metadata["provider"], "volc")
        self.assertNotIn("context_source", metadata)
        self.assertNotIn("max_output_source", metadata)

    def test_external_reasoning_levels_normalize_and_complete_light_through_max(self):
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "upstream_model": "glm-5.2",
            "supported_reasoning_levels": (" HIGH ", "low", " high", "MAX", "turbo", " ultra ", "xhigh", "MAX"),
            "default_reasoning_level": " MAX ",
        }

        model = catalog_sync.build_external_provider_model(external_model, self.policy, None)

        self.assertEqual(
            [item["effort"] for item in model["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(model["default_reasoning_level"], "max")

    def test_external_reasoning_default_ultra_falls_back_without_mapping_to_max(self):
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "upstream_model": "glm-5.2",
            "supported_reasoning_levels": ("low", "max", "xhigh"),
            "default_reasoning_level": " ultra ",
        }

        model = catalog_sync.build_external_provider_model(external_model, self.policy, None)

        self.assertEqual(model["default_reasoning_level"], "xhigh")

    def test_external_reasoning_sanitizes_fallback_template_ultra_metadata(self):
        fallback_template = {
            "supported_reasoning_levels": [
                {"effort": " Ultra ", "description": "must not leak"},
                {"effort": " HIGH ", "description": "fallback high"},
                {"effort": "high", "description": "duplicate"},
                {"effort": "turbo", "description": "unknown"},
            ],
            "default_reasoning_level": "ULTRA",
        }
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "upstream_model": "glm-5.2",
        }

        model = catalog_sync.build_external_provider_model(external_model, self.policy, fallback_template)

        self.assertEqual(
            [item["effort"] for item in model["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(model["default_reasoning_level"], "xhigh")
        self.assertNotIn("ultra", json.dumps(model).lower())

    def test_external_reasoning_uses_safe_defaults_when_fallback_has_no_valid_levels(self):
        fallback_template = {
            "supported_reasoning_levels": [{"effort": "ultra"}, {"effort": "turbo"}],
            "default_reasoning_level": "ultra",
        }
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "upstream_model": "glm-5.2",
        }

        model = catalog_sync.build_external_provider_model(external_model, self.policy, fallback_template)

        self.assertEqual(
            [item["effort"] for item in model["supported_reasoning_levels"]],
            ["low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(model["default_reasoning_level"], "xhigh")

    def test_sync_catalog_ignores_provider_alias_entries_for_external_catalog_state(self):
        providers = [
            ProviderConfig(
                id="volc",
                name="Volcengine",
                base_url="https://ark.example.test/v1",
                api_key="",
                display_prefix="Volc",
                sort_order=2,
                models=[
                    ModelConfig(id="glm-5.2", aliases=("GLM-5.2",), context_window=1024000),
                    ModelConfig(id="minimax-m3", context_window=512000),
                ],
            )
        ]
        policy = CatalogPolicy(
            denied_models=set(),
            denied_substrings=set(),
            display_names={},
            allowed_provider_models=("volc/glm-5.2", "volc/minimax-m3"),
        )
        written: dict[str, dict] = {}

        def capture_write(path: Path, data: dict) -> None:
            written[path.name] = data

        with (
            patch("catalog_sync.catalog_cache_is_fresh", return_value=False),
            patch("catalog_sync.load_policy", return_value=policy),
            patch("catalog_sync.load_include_official_models", return_value=False),
            patch("catalog_sync.load_official_model_sort_order", return_value=[]),
            patch("catalog_sync.load_official_disabled_models", return_value=[]),
            patch("catalog_sync.load_fallback_catalog_models", return_value=[]),
            patch("catalog_sync.read_client_version", return_value="0.142.0"),
            patch("catalog_sync.discover_ollama_ids", return_value=([], "test", "ok", "")),
            patch("catalog_sync.load_providers", return_value=providers),
            patch("catalog_sync.discover_ollama_model_metadata", return_value=({}, "")),
            patch("catalog_sync.load_previous_visible_models", return_value=set()),
            patch("catalog_sync.write_json", side_effect=capture_write),
        ):
            state = catalog_sync.sync_catalog()

        self.assertEqual(state["external_provider_models"], ["volc/glm-5.2", "volc/minimax-m3"])
        self.assertEqual(state["visible_models"], ["volc/glm-5.2", "volc/minimax-m3"])
        self.assertEqual(state["diff"], {"added": ["volc/glm-5.2", "volc/minimax-m3"], "removed": []})

        catalog = written[catalog_sync.GENERATED_CATALOG_FILENAME]
        priorities_by_slug = {model["slug"]: model["priority"] for model in catalog["models"]}
        self.assertEqual(priorities_by_slug, {"volc/glm-5.2": 200, "volc/minimax-m3": 201})

    def test_dynamic_ollama_metadata_overrides_static_context_and_modalities(self):
        metadata = {
            "kimi-k2.7-code": {
                "context_window": 262144,
                "context_source": "ollama_api_show",
                "capabilities": ["completion", "tools", "thinking", "vision"],
            }
        }

        catalog = build_codex_catalog(
            [],
            ["kimi-k2.7-code:cloud"],
            self.policy,
            "0.142.0",
            ollama_model_metadata=metadata,
        )
        kimi_model = next(model for model in catalog["models"] if model["slug"] == "kimi-k2.7-code")

        self.assertEqual(kimi_model["context_window"], 262144)
        self.assertEqual(kimi_model["max_context_window"], 262144)
        self.assertEqual(kimi_model["max_output_tokens"], 32768)
        self.assertEqual(kimi_model["input_modalities"], ["text", "image"])
        self.assertEqual(kimi_model["codex_proxy_metadata"]["context_source"], "ollama_api_show")

    def test_generated_paths_use_codex_home_when_imported(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            try:
                with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                    importlib.reload(catalog_sync)

                    self.assertEqual(
                        catalog_sync.GENERATED_CATALOG_PATH,
                        codex_home / "model-catalogs" / "codexhub-model-catalog.json",
                    )
                    self.assertEqual(
                        catalog_sync.LEGACY_GENERATED_CATALOG_PATH,
                        codex_home / "model-catalogs" / "codex-proxy-official-ollama.json",
                    )
                    self.assertEqual(
                        catalog_sync.GENERATED_STATE_PATH,
                        codex_home / "model-catalogs" / "codex-proxy-state.json",
                    )
                    self.assertEqual(catalog_sync.POLICY_PATH, repo_root / "config" / "catalog_policy.toml")
                    self.assertEqual(
                        catalog_sync.OLLAMA_FALLBACK_PATH,
                        repo_root / "model-catalogs" / "ollama-cloud.json",
                    )
            finally:
                importlib.reload(catalog_sync)

    def test_existing_generated_catalog_path_falls_back_to_legacy_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            try:
                with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                    importlib.reload(catalog_sync)
                    catalog_sync.LEGACY_GENERATED_CATALOG_PATH.parent.mkdir(parents=True)
                    catalog_sync.LEGACY_GENERATED_CATALOG_PATH.write_text('{"models":[]}', encoding="utf-8")

                    self.assertEqual(
                        catalog_sync.existing_generated_catalog_path(),
                        catalog_sync.LEGACY_GENERATED_CATALOG_PATH,
                    )

                    catalog_sync.GENERATED_CATALOG_PATH.write_text('{"models":[]}', encoding="utf-8")
                    self.assertEqual(
                        catalog_sync.existing_generated_catalog_path(),
                        catalog_sync.GENERATED_CATALOG_PATH,
                    )
            finally:
                importlib.reload(catalog_sync)

    def test_write_json_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "missing" / "model-catalogs" / "state.json"

            catalog_sync.write_json(target, {"ok": True})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})

    def test_write_json_uses_atomic_writer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "generated" / "catalog.json"
            calls: list[tuple[Path, str, str]] = []

            def capture_atomic_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
                calls.append((path, text, encoding))

            with patch.object(catalog_sync, "atomic_write_text", capture_atomic_write, create=True):
                catalog_sync.write_json(target, {"ok": True})

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], target)
            self.assertEqual(calls[0][2], "utf-8")
            self.assertEqual(json.loads(calls[0][1]), {"ok": True})

    def test_extracts_context_and_capabilities_from_ollama_show_payload(self):
        payload = {
            "capabilities": ["completion", "tools"],
            "model_info": {
                "general.architecture": "deepseek4",
                "deepseek4.context_length": 1048576,
            },
        }

        self.assertEqual(catalog_sync.extract_context_length(payload), 1048576)
        self.assertEqual(catalog_sync.extract_capabilities(payload), ["completion", "tools"])

    def test_diff_model_state_tracks_added_and_removed(self):
        diff = diff_model_state({"glm-5.2", "minimax-m3"}, {"glm-5.2", "kimi-k2.7-code"})
        self.assertEqual(diff["added"], ["kimi-k2.7-code"])
        self.assertEqual(diff["removed"], ["minimax-m3"])

    def test_discover_ollama_ids_uses_cloud_cache_without_local_cli_fallback(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("catalog_sync.discover_ollama_http") as discover_http,
            patch("catalog_sync.model_ids_from_catalog", return_value=["glm-5.2:cloud"]) as cache_models,
        ):
            ids, source, status, detail = discover_ollama_ids()

        self.assertEqual(ids, ["glm-5.2:cloud"])
        self.assertEqual(source, "ollama_cloud_cache")
        self.assertEqual(status, "missing_api_key_cache")
        self.assertEqual(detail, "OLLAMA_API_KEY is not set")
        discover_http.assert_not_called()
        cache_models.assert_called_once_with(catalog_sync.OLLAMA_FALLBACK_PATH)
        self.assertFalse(hasattr(catalog_sync, "discover_ollama_cli"))

    def test_model_ids_from_catalog_uses_runtime_fallback_when_bundled_catalog_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bundled_path = Path(tmpdir) / "missing.json"
            runtime_path = Path(tmpdir) / "runtime-ollama-cloud.json"
            runtime_path.write_text(
                json.dumps({"models": [{"slug": "glm-5.2"}, {"slug": "kimi-k2.7-code"}]}),
                encoding="utf-8",
            )
            with patch("catalog_sync.OLLAMA_FALLBACK_PATH", bundled_path), patch(
                "catalog_sync.RUNTIME_OLLAMA_FALLBACK_PATH",
                runtime_path,
            ):
                ids = catalog_sync.model_ids_from_catalog(bundled_path)

        self.assertEqual(ids, ["glm-5.2", "kimi-k2.7-code"])

    def test_discover_ollama_ids_reports_cloud_unavailable_without_key_or_cache(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("catalog_sync.discover_ollama_http") as discover_http,
            patch("catalog_sync.model_ids_from_catalog", return_value=[]),
        ):
            ids, source, status, detail = discover_ollama_ids()

        self.assertEqual(ids, [])
        self.assertEqual(source, "ollama_cloud_unavailable")
        self.assertEqual(status, "missing_api_key_unavailable")
        self.assertEqual(detail, "OLLAMA_API_KEY is not set")
        discover_http.assert_not_called()

    def test_discover_ollama_ids_reports_failed_cloud_auth_with_cache(self):
        fake_key = "fake-test-key-should-not-leak"
        error = HTTPError(
            url="https://ollama.com/v1/models",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with (
            patch.dict("os.environ", {"OLLAMA_API_KEY": fake_key}, clear=True),
            patch("catalog_sync.discover_ollama_http", side_effect=error) as discover_http,
            patch("catalog_sync.model_ids_from_catalog", return_value=["glm-5.2:cloud"]),
        ):
            ids, source, status, detail = discover_ollama_ids()

        self.assertEqual(ids, ["glm-5.2:cloud"])
        self.assertEqual(source, "ollama_cloud_cache")
        self.assertEqual(status, "http_failed_cache")
        self.assertEqual(detail, "HTTPError: 401")
        self.assertNotIn(fake_key, detail)
        discover_http.assert_called_once_with(fake_key)

    def test_discover_ollama_ids_reports_failed_cloud_auth_without_cache(self):
        fake_key = "fake-test-key-should-not-leak"
        error = HTTPError(
            url="https://ollama.com/v1/models",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with (
            patch.dict("os.environ", {"OLLAMA_API_KEY": fake_key}, clear=True),
            patch("catalog_sync.discover_ollama_http", side_effect=error),
            patch("catalog_sync.model_ids_from_catalog", return_value=[]),
        ):
            ids, source, status, detail = discover_ollama_ids()

        self.assertEqual(ids, [])
        self.assertEqual(source, "ollama_cloud_unavailable")
        self.assertEqual(status, "http_failed_unavailable")
        self.assertEqual(detail, "HTTPError: 401")
        self.assertNotIn(fake_key, detail)

    def test_discover_ollama_ids_reports_empty_cloud_response_with_cache(self):
        with (
            patch.dict("os.environ", {"OLLAMA_API_KEY": "fake-test-key"}, clear=True),
            patch("catalog_sync.discover_ollama_http", return_value=[]),
            patch("catalog_sync.model_ids_from_catalog", return_value=["glm-5.2:cloud"]),
        ):
            ids, source, status, detail = discover_ollama_ids()

        self.assertEqual(ids, ["glm-5.2:cloud"])
        self.assertEqual(source, "ollama_cloud_cache")
        self.assertEqual(status, "http_empty_cache")
        self.assertEqual(detail, "cloud HTTP returned 0 models")

    def test_discover_ollama_ids_reports_empty_cloud_response_without_cache(self):
        with (
            patch.dict("os.environ", {"OLLAMA_API_KEY": "fake-test-key"}, clear=True),
            patch("catalog_sync.discover_ollama_http", return_value=[]),
            patch("catalog_sync.model_ids_from_catalog", return_value=[]),
        ):
            ids, source, status, detail = discover_ollama_ids()

        self.assertEqual(ids, [])
        self.assertEqual(source, "ollama_cloud_unavailable")
        self.assertEqual(status, "http_empty_unavailable")
        self.assertEqual(detail, "cloud HTTP returned 0 models")

    def test_discover_ollama_ids_reports_json_failure_without_leaking_key(self):
        fake_key = "fake-json-key-should-not-leak"
        error = json.JSONDecodeError(f"bad response for {fake_key}", "", 0)
        with (
            patch.dict("os.environ", {"OLLAMA_API_KEY": fake_key}, clear=True),
            patch("catalog_sync.discover_ollama_http", side_effect=error),
            patch("catalog_sync.model_ids_from_catalog", return_value=["glm-5.2:cloud"]),
        ):
            ids, source, status, detail = discover_ollama_ids()

        self.assertEqual(ids, ["glm-5.2:cloud"])
        self.assertEqual(source, "ollama_cloud_cache")
        self.assertEqual(status, "http_failed_cache")
        self.assertEqual(detail, "JSONDecodeError")
        self.assertNotIn(fake_key, detail)

    def test_load_include_official_models_defaults_true_when_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"CODEX_HOME": tmpdir}, clear=False):
                import importlib
                import catalog_sync
                importlib.reload(catalog_sync)
                self.assertTrue(catalog_sync.load_include_official_models())
                importlib.reload(catalog_sync)

    def test_load_include_official_models_reads_false_from_settings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            settings_path = codex_home / "proxy" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text('{"include_official_models": false}', encoding="utf-8")
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                import importlib
                import catalog_sync
                importlib.reload(catalog_sync)
                self.assertFalse(catalog_sync.load_include_official_models())
                importlib.reload(catalog_sync)

    def test_load_official_model_sort_order_reads_string_list(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            settings_path = codex_home / "proxy" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "official_model_sort_order": [
                            "openai/gpt-5.4",
                            " gpt-5.4 ",
                            "gpt-5.5",
                            "",
                            123,
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                import importlib
                import catalog_sync
                importlib.reload(catalog_sync)
                self.assertEqual(
                    catalog_sync.load_official_model_sort_order(),
                    ["gpt-5.4", "gpt-5.5"],
                )
                importlib.reload(catalog_sync)


if __name__ == "__main__":
    unittest.main()

