import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from catalog import CatalogPolicy, load_policy
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

    def test_official_visibility_contract_excludes_hidden_unknown_and_internal_duplicates(self):
        official = [
            {
                "slug": "gpt-5.6-terra",
                "display_name": "GPT-5.6 Terra",
                "visibility": "list",
            },
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6 Sol",
                "visibility": "hide",
            },
            {
                "id": "codex-auto-review",
                "model": "gpt-5.6-terra",
                "slug": "gpt-5.6-terra",
                "display_name": "GPT-5.6 Terra (review)",
                "visibility": "list",
            },
            {
                "slug": "gpt-5.6-luna",
                "display_name": "GPT-5.6 Luna",
                "visibility": "future",
            },
        ]

        catalog = build_codex_catalog(official, [], self.policy, "0.144.0")

        self.assertEqual(
            [model["slug"] for model in catalog["models"]],
            ["gpt-5.6-terra"],
        )
        self.assertEqual(catalog["models"][0]["visibility"], "list")

    def test_hidden_flag_overrides_list_visibility(self):
        catalog = build_codex_catalog(
            [
                {
                    "slug": "gpt-5.6-terra",
                    "visibility": "list",
                    "hidden": True,
                }
            ],
            [],
            self.policy,
            "0.144.0",
        )

        self.assertEqual(catalog["models"], [])
        self.assertEqual(catalog["visibility_diagnostics"]["hidden"], 1)

    def test_official_seed_snapshot_fails_closed_for_unknown_visibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_path = Path(tmp) / "runtime-seed.json"
            seed_path.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000,
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "visibility": "list",
                            },
                            {
                                "slug": "gpt-5.6-sol",
                                "visibility": "hide",
                            },
                            {
                                "id": "codex-auto-review",
                                "slug": "gpt-5.6-terra",
                                "model": "gpt-5.6-terra",
                                "visibility": "list",
                            },
                            {
                                "slug": "gpt-5.6-luna",
                                "visibility": "future",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = catalog_sync.load_official_seed_snapshot(
                Path(tmp) / "missing-bundled.json",
                runtime_path=seed_path,
                now_timestamp=1_001,
            )

        self.assertEqual([model["slug"] for model in snapshot.models], ["gpt-5.6-terra"])
        self.assertEqual(snapshot.models[0]["visibility"], "list")

    def test_runtime_seed_all_hidden_does_not_fallback_to_bundled_or_policy_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = Path(tmp) / "runtime-seed.json"
            runtime_path.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000,
                        "models": [
                            {"slug": "gpt-5.6-terra", "visibility": "hide"},
                            {"slug": "gpt-5.6-sol", "visibility": "future"},
                            {
                                "id": "codex-auto-review",
                                "slug": "gpt-5.6-terra",
                                "visibility": "list",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            bundled_path = Path(tmp) / "bundled-seed.json"
            bundled_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-5.5", "visibility": "list"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            snapshot = catalog_sync.load_official_seed_snapshot(
                bundled_path,
                runtime_path=runtime_path,
                now_timestamp=1_001,
            )
            catalog = build_codex_catalog(
                snapshot.models,
                [],
                self.policy,
                "0.144.0",
                official_source_present=snapshot.source_present,
                visibility_diagnostics=snapshot.visibility_diagnostics,
            )

        self.assertEqual(snapshot.models, [])
        self.assertTrue(snapshot.source_present)
        self.assertEqual(snapshot.visibility_diagnostics, {"hidden": 1, "unknown": 1, "internal": 1})
        self.assertEqual([model["slug"] for model in catalog["models"]], [])
        self.assertEqual(catalog["visibility_diagnostics"], snapshot.visibility_diagnostics)

    def test_seed_uses_bounded_upstream_visibility_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_path = Path(tmp) / "runtime-seed.json"
            runtime_path.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000,
                        "visibility_diagnostics": {
                            "hidden": 101,
                            "unknown": 2,
                            "internal": 1,
                        },
                        "models": [{"slug": "gpt-5.6-terra", "visibility": "list"}],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = catalog_sync.load_official_seed_snapshot(
                Path(tmp) / "missing-bundled.json",
                runtime_path=runtime_path,
                now_timestamp=1_001,
            )

        self.assertEqual(
            snapshot.visibility_diagnostics,
            {"hidden": 100, "unknown": 2, "internal": 1},
        )

    def test_unknown_official_source_does_not_recreate_policy_models(self):
        catalog = build_codex_catalog(
            [{"slug": "gpt-5.6-terra", "visibility": "future"}],
            [],
            self.policy,
            "0.144.0",
        )

        self.assertEqual([model["slug"] for model in catalog["models"]], [])
        self.assertEqual(catalog["visibility_diagnostics"]["unknown"], 1)

    def test_direct_cache_internal_duplicate_terra_is_ignored_for_authority(self):
        fixture_path = Path(__file__).parent / "fixtures" / "codex_0_144_2_direct_models_cache.json"
        cache_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        cache_payload["models"].append(
            {
                "id": "codex-auto-review",
                "model": "gpt-5.6-terra",
                "slug": "gpt-5.6-terra",
                "visibility": "list",
            }
        )
        snapshot = catalog_sync.OfficialSeedSnapshot(
            models=[
                {**model, "visibility": "list"}
                for model in cache_payload["models"]
                if str(model.get("slug", "")).startswith("gpt-")
            ],
            source="current_direct_official",
            context_freshness="fresh",
        )
        cache_timestamp = catalog_sync._catalog_fetched_at_timestamp(cache_payload["fetched_at"])
        self.assertIsNotNone(cache_timestamp)

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "models_cache.json"
            cache_path.write_text(json.dumps(cache_payload), encoding="utf-8")
            authority = catalog_sync.load_fresh_direct_official_cache_authority(
                snapshot,
                cache_path,
                now_timestamp=cache_timestamp + 1,
            )

        self.assertEqual(authority.freshness, "fresh")
        self.assertEqual(
            sorted(authority.context_by_slug),
            [
                "gpt-5.3-codex-spark",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.5",
                "gpt-5.6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
            ],
        )

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

    def test_discovered_kimi_k26_remains_visible_without_beta1_qualification(self):
        policy = load_policy(Path("config/catalog_policy.toml"))
        catalog = build_codex_catalog(
            [],
            ["kimi-k2.6:cloud", "kimi-k2.7-code:cloud"],
            policy,
            "0.142.0",
        )

        by_slug = {model["slug"]: model for model in catalog["models"]}
        self.assertEqual(
            [model["slug"] for model in catalog["models"] if model["slug"].startswith("kimi-")],
            ["kimi-k2.6", "kimi-k2.7-code"],
        )
        self.assertEqual(by_slug["kimi-k2.6"]["display_name"], "Ollama Kimi K2.6")
        self.assertEqual(by_slug["kimi-k2.7-code"]["display_name"], "Ollama Kimi K2.7 Code")
        for slug in ("kimi-k2.6", "kimi-k2.7-code"):
            with self.subTest(slug=slug):
                metadata = by_slug[slug].get("codex_proxy_metadata", {})
                self.assertNotIn("native_responses_tool_codec", metadata)
                self.assertNotIn("tool_surface_strategy", metadata)

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
        self.assertNotIn("max_context_window", by_slug["gpt-5.4-mini"])
        self.assertEqual(
            by_slug["gpt-5.4-mini"]["codex_proxy_metadata"]["official_context_budget"],
            {"source": "missing", "freshness": "missing"},
        )
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

    def test_official_catalog_preserves_app_metadata_and_projects_cli_upgrade_schema(self):
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
                "tool_mode": "code_mode_only",
                "model_messages": {"upgrade": "Use Sol"},
                "skills_instructions": "Official skills contract",
                "web_search_tool_type": "text",
                "use_responses_lite": True,
                "availability": {"plan": "plus"},
                "upgrade": "gpt-5.7-sol",
                "upgradeInfo": {
                    "model": "gpt-5.7-sol",
                    "upgradeCopy": None,
                    "modelLink": None,
                    "migrationMarkdown": "Switch to GPT-5.7 Sol.\n",
                },
                "comp_hash": "3000",
            }
        ]

        catalog = build_codex_catalog(
            official,
            [],
            self.policy,
            "0.144.0",
            official_context_signals={
                "gpt-5.6-sol": {
                    "context_window": 400_000,
                    "effective_context_window_percent": 100,
                    "freshness": "fresh",
                    "source": "current_direct_official",
                }
            },
        )
        model = catalog["models"][0]

        self.assertEqual(model["slug"], "gpt-5.6-sol")
        self.assertEqual(model["display_name"], "5.6 Sol")
        for key, value in official[0].items():
            if key not in {"slug", "display_name", "upgrade"}:
                self.assertEqual(model[key], value, key)
        self.assertEqual(
            model["upgrade"],
            {
                "model": "gpt-5.7-sol",
                "migration_markdown": "Switch to GPT-5.7 Sol.\n",
            },
        )
        self.assertEqual(model["shell_type"], "shell_command")
        self.assertEqual(model["supports_parallel_tool_calls"], True)
        self.assertEqual(model["default_reasoning_level"], "medium")
        self.assertIn("base_instructions", model)
        self.assertEqual(model["supported_in_api"], True)
        self.assertEqual(model["codex_proxy_metadata"]["provider"], "openai")
        self.assertEqual(model["codex_proxy_metadata"]["upstream_name"], "official")
        self.assertEqual(model["codex_proxy_metadata"]["upstream_model"], "gpt-5.6-sol")
        self.assertEqual(
            model["codex_proxy_metadata"]["official_context_budget"]["source"],
            "current_direct_official",
        )

    def test_official_catalog_preserves_native_cli_upgrade_schema(self):
        native_upgrade = {
            "model": "gpt-5.7-sol",
            "migration_markdown": "Switch to GPT-5.7 Sol.\n",
        }
        catalog = build_codex_catalog(
            [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "upgrade": native_upgrade,
                }
            ],
            [],
            self.policy,
            "0.145.0",
        )

        self.assertEqual(catalog["models"][0]["upgrade"], native_upgrade)

    def test_official_catalog_backfills_pinned_planner_metadata_per_model(self):
        official = [
            {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol"},
            {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra"},
            {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna"},
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "use_responses_lite": True,
                "prefer_websockets": False,
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v2",
            },
            {
                "slug": "gpt-5.4",
                "display_name": "GPT-5.4",
                "use_responses_lite": True,
                "prefer_websockets": False,
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v2",
            },
            {
                "slug": "gpt-5.4-mini",
                "display_name": "GPT-5.4-Mini",
                "use_responses_lite": True,
                "prefer_websockets": False,
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v2",
            },
            {
                "slug": "gpt-5.3-codex-spark",
                "display_name": "GPT-5.3-Codex-Spark",
                "use_responses_lite": True,
            },
            {"slug": "gpt-sparse", "display_name": "GPT-Sparse"},
        ]

        catalog = build_codex_catalog(
            official,
            [],
            self.policy,
            "0.144.0",
            official_context_signals={
                "gpt-5.6-sol": {
                    "context_window": 400_000,
                    "effective_context_window_percent": 100,
                    "freshness": "fresh",
                    "source": "current_direct_official",
                }
            },
        )
        by_slug = {model["slug"]: model for model in catalog["models"]}

        for slug, multi_agent_version in (
            ("gpt-5.6-sol", "v2"),
            ("gpt-5.6-terra", "v2"),
            ("gpt-5.6-luna", "v1"),
        ):
            with self.subTest(slug=slug):
                model = by_slug[slug]
                self.assertEqual(model["tool_mode"], "code_mode_only")
                self.assertEqual(model["multi_agent_version"], multi_agent_version)
                self.assertIs(model["prefer_websockets"], True)
                self.assertIs(model["use_responses_lite"], True)

        for slug in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"):
            with self.subTest(slug=slug):
                model = by_slug[slug]
                self.assertIn("tool_mode", model)
                self.assertIsNone(model["tool_mode"])
                self.assertIn("multi_agent_version", model)
                self.assertIsNone(model["multi_agent_version"])
                self.assertIs(model["prefer_websockets"], True)
                self.assertIs(model["use_responses_lite"], False)

        for slug in ("gpt-5.3-codex-spark", "gpt-sparse"):
            with self.subTest(slug=slug):
                model = by_slug[slug]
                self.assertIs(model["use_responses_lite"], False)
                self.assertNotIn("prefer_websockets", model)
                self.assertNotIn("tool_mode", model)
                self.assertNotIn("multi_agent_version", model)

    def test_minimal_official_catalog_uses_pinned_planner_metadata(self):
        policy = CatalogPolicy(
            denied_models=set(),
            denied_substrings=set(),
            display_names={},
            official_models=(
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.3-codex-spark",
                "gpt-sparse",
            ),
            allowed_ollama_cloud_models=(),
        )

        catalog = build_codex_catalog([], [], policy, "0.144.0")
        by_slug = {model["slug"]: model for model in catalog["models"]}

        self.assertEqual(by_slug["gpt-5.6-sol"]["multi_agent_version"], "v2")
        self.assertEqual(by_slug["gpt-5.6-terra"]["multi_agent_version"], "v2")
        self.assertEqual(by_slug["gpt-5.6-luna"]["multi_agent_version"], "v1")
        self.assertIs(by_slug["gpt-5.5"]["use_responses_lite"], False)
        self.assertIsNone(by_slug["gpt-5.5"]["tool_mode"])
        self.assertIsNone(by_slug["gpt-5.5"]["multi_agent_version"])
        for slug in ("gpt-5.4", "gpt-5.4-mini"):
            with self.subTest(slug=slug):
                self.assertIs(by_slug[slug]["use_responses_lite"], False)
                self.assertIs(by_slug[slug]["prefer_websockets"], True)
                self.assertIsNone(by_slug[slug]["tool_mode"])
                self.assertIsNone(by_slug[slug]["multi_agent_version"])
        for slug in ("gpt-5.3-codex-spark", "gpt-sparse"):
            with self.subTest(slug=slug):
                self.assertIs(by_slug[slug]["use_responses_lite"], False)
                self.assertNotIn("prefer_websockets", by_slug[slug])
                self.assertNotIn("tool_mode", by_slug[slug])
                self.assertNotIn("multi_agent_version", by_slug[slug])

    def test_sync_catalog_preserves_legacy_luna_multi_agent_override_across_restart(self):
        official = [
            {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            generated = root / "model-catalogs" / catalog_sync.GENERATED_CATALOG_FILENAME
            baseline = root / "model-catalogs" / catalog_sync.MANAGED_CATALOG_BASELINE_FILENAME
            overrides = root / "model-catalogs" / catalog_sync.CATALOG_OVERRIDES_FILENAME
            state_path = root / "model-catalogs" / "state.json"

            def run_sync() -> dict:
                with (
                    patch.object(catalog_sync, "GENERATED_CATALOG_PATH", generated),
                    patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline),
                    patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides),
                    patch.object(catalog_sync, "GENERATED_STATE_PATH", state_path),
                    patch("catalog_sync.catalog_cache_is_fresh", return_value=False),
                    patch("catalog_sync.load_policy", return_value=self.policy),
                    patch("catalog_sync.load_include_official_models", return_value=True),
                    patch("catalog_sync.load_official_model_sort_order", return_value=[]),
                    patch("catalog_sync.load_official_disabled_models", return_value=[]),
                    patch("catalog_sync.load_official_seed_snapshot", return_value=catalog_sync.OfficialSeedSnapshot(official, "direct", "fresh", True)),
                    patch("catalog_sync.load_previous_official_context_budgets", return_value={}),
                    patch("catalog_sync.load_fresh_direct_official_cache_authority", return_value=None),
                    patch("catalog_sync.official_context_signals_from_snapshot", return_value={}),
                    patch("catalog_sync.load_fallback_catalog_models", return_value=[]),
                    patch("catalog_sync.read_client_version", return_value="0.146.0"),
                    patch("catalog_sync.discover_ollama_ids", return_value=([], "test", "ok", "")),
                    patch("catalog_sync.load_providers", return_value=[]),
                    patch("catalog_sync.catalog_visible_ollama_cloud_models", return_value=(False, [])),
                    patch("catalog_sync.catalog_visible_external_models", return_value=[]),
                    patch("catalog_sync.discover_ollama_model_metadata", return_value=({}, "")),
                ):
                    return catalog_sync.sync_catalog()

            run_sync()
            # Remove the new sidecars to model a pre-Beta2 single-file
            # installation.  The next sync must migrate only the supported
            # same-model planner delta from the edited effective catalog.
            baseline.unlink()
            overrides.unlink()
            initial = json.loads(generated.read_text(encoding="utf-8"))
            # Strip the Beta2 ownership marker so this is a true legacy
            # single-file migration rather than a modern catalog round-trip.
            initial_metadata = initial["models"][0]["codex_proxy_metadata"]
            initial_metadata.pop(catalog_sync.CATALOG_OWNER_METADATA_KEY, None)
            initial_metadata.pop(catalog_sync.CATALOG_OWNER_METADATA_VERSION_KEY, None)
            initial_metadata.pop(catalog_sync.CATALOG_OWNER_IDENTITY_KEY, None)
            initial_metadata.pop(catalog_sync.CATALOG_OWNER_SIGNATURE_KEY, None)
            initial["models"][0]["multi_agent_version"] = "v2"
            # Pre-Beta2 Ollama rows may not carry proxy identity metadata;
            # they must not prevent the exact Official legacy migration.
            initial["models"].append(
                {
                    "slug": "glm-5.2",
                    "visibility": "list",
                    "use_responses_lite": False,
                }
            )
            generated.write_text(json.dumps(initial), encoding="utf-8")

            state = run_sync()
            effective = json.loads(generated.read_text(encoding="utf-8"))
            managed = json.loads(baseline.read_text(encoding="utf-8"))
            override_state = json.loads(overrides.read_text(encoding="utf-8"))

            self.assertEqual(effective["models"][0]["multi_agent_version"], "v2")
            self.assertEqual(managed["models"][0]["multi_agent_version"], "v1")
            self.assertEqual(override_state["overrides"][0]["fields"], {"multi_agent_version": "v2"})
            self.assertEqual(state["catalog_override_diagnostics"]["accepted"], 1)

            # A second restart is idempotent and must not duplicate the sidecar
            # entry or drift the effective value.
            run_sync()
            repeated = json.loads(generated.read_text(encoding="utf-8"))
            repeated_overrides = json.loads(overrides.read_text(encoding="utf-8"))
            self.assertEqual(repeated["models"][0]["multi_agent_version"], "v2")
            self.assertEqual(len(repeated_overrides["overrides"]), 1)

            # A hidden source row is not an ownership authority.  The next
            # sync must fail closed instead of copying its v2 value onto the
            # newly generated visible row.
            hidden = json.loads(generated.read_text(encoding="utf-8"))
            hidden["models"][0]["visibility"] = "hide"
            generated.write_text(json.dumps(hidden), encoding="utf-8")
            state = run_sync()
            effective = json.loads(generated.read_text(encoding="utf-8"))
            override_state = json.loads(overrides.read_text(encoding="utf-8"))
            self.assertEqual(effective["models"][0]["multi_agent_version"], "v1")
            self.assertEqual(override_state["overrides"], [])
            self.assertEqual(
                state["catalog_override_diagnostics"]["reasons"].get("invalid_row_identity"),
                1,
            )

    def test_catalog_override_does_not_copy_official_planner_metadata_to_external_model(self):
        external = {
            "alias": "volc/gpt-5.6-luna",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "upstream_model": "gpt-5.6-luna",
        }
        model = catalog_sync.build_external_provider_model(
            external,
            self.policy,
            {
                "prefer_websockets": True,
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v2",
                "use_responses_lite": True,
                "codex_proxy_metadata": {
                    "official_context_budget": {"context_window": 272000},
                    "provider": "openai",
                },
            },
        )
        self.assertNotIn("multi_agent_version", model)
        self.assertNotIn("tool_mode", model)
        self.assertNotIn("prefer_websockets", model)
        self.assertIs(model["use_responses_lite"], False)
        self.assertNotIn("official_context_budget", model["codex_proxy_metadata"])
        self.assertEqual(model["codex_proxy_metadata"]["provider"], "volc")
        external_identity = catalog_sync.catalog_model_identity(model)
        self.assertIsNotNone(external_identity)
        self.assertTrue(
            catalog_sync._catalog_override_row_is_eligible(
                external_identity,
                model,
            )
        )

        ollama = catalog_sync.build_ollama_model(
            "glm-5.2",
            self.policy,
            {},
            {
                "prefer_websockets": True,
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v2",
                "use_responses_lite": True,
            },
        )
        self.assertNotIn("multi_agent_version", ollama)
        self.assertNotIn("tool_mode", ollama)
        self.assertNotIn("prefer_websockets", ollama)
        self.assertIs(ollama["use_responses_lite"], False)
        self.assertEqual(ollama["codex_proxy_metadata"]["provider"], "ollama-cloud")
        self.assertEqual(ollama["codex_proxy_metadata"]["upstream_name"], "ollama_cloud")
        self.assertEqual(ollama["codex_proxy_metadata"]["upstream_model"], "glm-5.2")

    def test_catalog_override_rejects_invalid_unknown_and_cross_provider_entries(self):
        valid_identity = ("openai", "official", "gpt-5.6-luna")
        cases = {
            "external-provider": {
                "provider": "volc",
                "upstream_name": "official",
                "upstream_model": "gpt-5.6-luna",
                "fields": {"multi_agent_version": "v2"},
            },
            "unknown-official": {
                "provider": "openai",
                "upstream_name": "official",
                "upstream_model": "gpt-9.9-luna",
                "fields": {"multi_agent_version": "v2"},
            },
            "invalid-shape": {
                "provider": "openai",
                "upstream_name": "official",
                "upstream_model": "gpt-5.6-luna",
                "fields": {"use_responses_lite": False},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "overrides.json"
            path.write_text(
                json.dumps({"schema_version": 1, "overrides": list(cases.values())}),
                encoding="utf-8",
            )
            diagnostics = {}
            self.assertEqual(catalog_sync._load_catalog_override_state(path, diagnostics), {})
            self.assertEqual(diagnostics["reasons"]["invalid_override_fields"], 1)
            self.assertTrue(
                catalog_sync._planner_override_is_valid(
                    valid_identity,
                    {"multi_agent_version": "v2"},
                )
            )
            for field in ("prefer_websockets", "use_responses_lite"):
                with self.subTest(field=field):
                    self.assertFalse(
                        catalog_sync._planner_override_is_valid(
                            valid_identity,
                            {field: 1},
                        )
                    )
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(catalog_sync._load_catalog_override_state(path), {})

            valid_entry = {
                "provider": valid_identity[0],
                "upstream_name": valid_identity[1],
                "upstream_model": valid_identity[2],
                "fields": {"multi_agent_version": "v2"},
            }
            diagnostics = {}
            path.write_text(
                json.dumps({"schema_version": True, "overrides": [valid_entry]}),
                encoding="utf-8",
            )
            self.assertEqual(catalog_sync._load_catalog_override_state(path, diagnostics), {})
            self.assertEqual(diagnostics["reasons"]["invalid_sidecar"], 1)

            diagnostics = {}
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "overrides": [
                            valid_entry,
                            {**valid_entry, "unexpected": "field"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(catalog_sync._load_catalog_override_state(path, diagnostics), {})
            self.assertEqual(diagnostics["reasons"]["invalid_sidecar"], 1)

            diagnostics = {}
            path.write_text(
                json.dumps({"schema_version": 1, "overrides": [valid_entry, valid_entry]}),
                encoding="utf-8",
            )
            self.assertEqual(catalog_sync._load_catalog_override_state(path, diagnostics), {})
            self.assertEqual(diagnostics["reasons"]["invalid_sidecar"], 1)

    def test_catalog_override_rejects_malformed_managed_baseline_without_legacy_migration(self):
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            baseline = Path(tmpdir) / "baseline.json"
            generated = Path(tmpdir) / "catalog.json"
            baseline.write_text(json.dumps({"models": []}), encoding="utf-8")
            generated.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-luna",
                                "visibility": "list",
                                "multi_agent_version": "v2",
                                "codex_proxy_metadata": {
                                    "provider": identity[0],
                                    "upstream_name": identity[1],
                                    "upstream_model": identity[2],
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", generated),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", Path(tmpdir) / "overrides.json"),
            ):
                overrides, diagnostics = catalog_sync._collect_catalog_overrides()
            self.assertEqual(overrides, {})
            self.assertEqual(diagnostics["reasons"]["invalid_baseline"], 1)

            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "managed_baseline": True,
                        "models": [
                            {
                                "slug": "gpt-5.6-luna",
                                "visibility": "list",
                                "codex_proxy_metadata": {
                                    "provider": identity[0],
                                    "upstream_name": identity[1],
                                    "upstream_model": identity[2],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", generated),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", Path(tmpdir) / "overrides.json"),
            ):
                overrides, diagnostics = catalog_sync._collect_catalog_overrides()
            self.assertEqual(overrides, {})
            self.assertEqual(diagnostics["reasons"]["invalid_baseline"], 1)

    def test_catalog_override_matches_exact_identity_not_slug(self):
        official_identity = ("openai", "official", "gpt-5.6-luna")
        external_model = {
            "slug": "volc/gpt-5.6-luna",
            "codex_proxy_metadata": {
                "provider": "volc",
                "upstream_name": "volcengine",
                "upstream_model": "gpt-5.6-luna",
            },
        }
        catalog = {"models": [external_model]}
        catalog_sync._apply_catalog_overrides(
            catalog,
            {official_identity: {"multi_agent_version": "v2"}},
            {"accepted": 0, "rejected": 0},
        )
        self.assertNotIn("multi_agent_version", external_model)

    def test_catalog_override_rejects_legacy_official_identity_spoof(self):
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current = root / "catalog.json"
            current.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": identity[2],
                                "visibility": "list",
                                "multi_agent_version": "v2",
                                "codex_proxy_metadata": {
                                    "provider": identity[0],
                                    "upstream_name": identity[1],
                                    "upstream_model": identity[2],
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", root / "missing-baseline.json"),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", root / "overrides.json"),
            ):
                overrides, diagnostics = catalog_sync._collect_catalog_overrides()

            self.assertEqual(overrides, {})
            self.assertEqual(diagnostics["reasons"]["invalid_row_identity"], 1)

    def test_catalog_override_validates_managed_identity_digest(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        managed = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
        current = json.loads(json.dumps(managed))
        current["models"][0]["multi_agent_version"] = "v2"
        current["models"][0]["codex_proxy_metadata"][catalog_sync.CATALOG_OWNER_IDENTITY_KEY] = "0" * 64
        baseline = json.loads(json.dumps(managed))
        baseline.update({"schema_version": 1, "managed_baseline": True})

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current_path = root / "catalog.json"
            baseline_path = root / "baseline.json"
            current_path.write_text(json.dumps(current), encoding="utf-8")
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            with (
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline_path),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", root / "overrides.json"),
            ):
                overrides, diagnostics = catalog_sync._collect_catalog_overrides()

            self.assertEqual(overrides, {})
            self.assertEqual(diagnostics["reasons"]["invalid_row_identity"], 1)

        target = json.loads(json.dumps(managed["models"][0]))
        target["codex_proxy_metadata"][catalog_sync.CATALOG_OWNER_IDENTITY_KEY] = "0" * 64
        catalog = {"models": [target]}
        diagnostics = {"accepted": 0, "rejected": 0, "reasons": {}}
        catalog_sync._apply_catalog_overrides(
            catalog,
            {("openai", "official", "gpt-5.6-luna"): {"multi_agent_version": "v2"}},
            diagnostics,
        )
        self.assertNotEqual(target.get("multi_agent_version"), "v2")
        self.assertEqual(diagnostics["reasons"]["invalid_row_identity"], 1)

    def test_catalog_override_rejects_recomputed_public_digest_without_owner_signature(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overrides_path = root / "overrides.json"
            key_path = root / catalog_sync.CATALOG_OWNER_SECRET_FILENAME
            key_path.write_text("11" * 32, encoding="ascii")
            with patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides_path), patch.object(
                catalog_sync, "_CATALOG_OWNER_SECRET_CACHE", None
            ):
                managed = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
                current = json.loads(json.dumps(managed))
                current["models"][0]["multi_agent_version"] = "v2"
                current["models"][0]["description"] = "forged Official row"
                metadata = current["models"][0]["codex_proxy_metadata"]
                metadata[catalog_sync.CATALOG_OWNER_IDENTITY_KEY] = catalog_sync.catalog_identity_digest(identity)
                baseline = json.loads(json.dumps(managed))
                baseline.update({"schema_version": 1, "managed_baseline": True})
                current_path = root / "catalog.json"
                baseline_path = root / "baseline.json"
                current_path.write_text(json.dumps(current), encoding="utf-8")
                baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
                with (
                    patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                    patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline_path),
                ):
                    collected, diagnostics = catalog_sync._collect_catalog_overrides()

            self.assertEqual(collected, {})
            self.assertEqual(diagnostics["reasons"]["invalid_row_identity"], 1)

    def test_marker_only_beta2_baseline_migrates_existing_luna_override(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overrides_path = root / "overrides.json"
            (root / catalog_sync.CATALOG_OWNER_SECRET_FILENAME).write_text(
                "22" * 32,
                encoding="ascii",
            )
            with (
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides_path),
                patch.object(catalog_sync, "_CATALOG_OWNER_SECRET_CACHE", None),
            ):
                managed = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
                # This is the representation emitted by the pre-HMAC Beta2
                # build: it has CodexHub ownership metadata but no signature.
                for model in managed["models"]:
                    model["codex_proxy_metadata"].pop(
                        catalog_sync.CATALOG_OWNER_SIGNATURE_KEY,
                        None,
                    )
                baseline = json.loads(json.dumps(managed))
                baseline.update({"schema_version": 1, "managed_baseline": True})
                current = json.loads(json.dumps(managed))
                current["models"][0]["multi_agent_version"] = "v2"
                baseline_path = root / "baseline.json"
                current_path = root / "catalog.json"
                baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
                current_path.write_text(json.dumps(current), encoding="utf-8")
                with (
                    patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                    patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline_path),
                ):
                    collected, diagnostics = catalog_sync._collect_catalog_overrides()

            self.assertEqual(
                collected,
                {identity: {"multi_agent_version": "v2"}},
            )
            self.assertEqual(diagnostics["accepted"], 1)

    def test_marker_only_legacy_catalog_migrates_against_fresh_generated_baseline(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overrides_path = root / "overrides.json"
            current_path = root / "catalog.json"
            (root / catalog_sync.CATALOG_OWNER_SECRET_FILENAME).write_text(
                "55" * 32,
                encoding="ascii",
            )
            with (
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides_path),
                patch.object(catalog_sync, "_CATALOG_OWNER_SECRET_CACHE", None),
            ):
                generated = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
                current = json.loads(json.dumps(generated))
                current["models"][0]["multi_agent_version"] = "v2"
                current["models"][0]["codex_proxy_metadata"].pop(
                    catalog_sync.CATALOG_OWNER_SIGNATURE_KEY,
                    None,
                )
                current_path.write_text(json.dumps(current), encoding="utf-8")
                with (
                    patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                    patch.object(
                        catalog_sync,
                        "MANAGED_CATALOG_BASELINE_PATH",
                        root / "missing-baseline.json",
                    ),
                ):
                    collected, diagnostics = catalog_sync._collect_catalog_overrides(
                        legacy_baseline=generated,
                    )

            self.assertEqual(
                collected,
                {identity: {"multi_agent_version": "v2"}},
            )
            self.assertEqual(diagnostics["accepted"], 1)

    def test_generated_legacy_baseline_rejects_markerless_forged_row(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overrides_path = root / "overrides.json"
            current_path = root / "catalog.json"
            (root / catalog_sync.CATALOG_OWNER_SECRET_FILENAME).write_text(
                "66" * 32,
                encoding="ascii",
            )
            with (
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides_path),
                patch.object(catalog_sync, "_CATALOG_OWNER_SECRET_CACHE", None),
            ):
                generated = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
                current = json.loads(json.dumps(generated))
                row = current["models"][0]
                row["multi_agent_version"] = "v2"
                row["description"] = "forged Official row"
                metadata = row["codex_proxy_metadata"]
                for key in (
                    catalog_sync.CATALOG_OWNER_METADATA_KEY,
                    catalog_sync.CATALOG_OWNER_METADATA_VERSION_KEY,
                    catalog_sync.CATALOG_OWNER_IDENTITY_KEY,
                    catalog_sync.CATALOG_OWNER_SIGNATURE_KEY,
                ):
                    metadata.pop(key, None)
                current_path.write_text(json.dumps(current), encoding="utf-8")
                with (
                    patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                    patch.object(
                        catalog_sync,
                        "MANAGED_CATALOG_BASELINE_PATH",
                        root / "missing-baseline.json",
                    ),
                ):
                    collected, diagnostics = catalog_sync._collect_catalog_overrides(
                        legacy_baseline=generated,
                    )

            self.assertEqual(collected, {})
            self.assertEqual(diagnostics["reasons"]["invalid_row_identity"], 1)

    def test_marker_only_catalog_without_baseline_rejects_forged_row_when_key_exists(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            overrides_path = root / "overrides.json"
            current_path = root / "catalog.json"
            (root / catalog_sync.CATALOG_OWNER_SECRET_FILENAME).write_text(
                "33" * 32,
                encoding="ascii",
            )
            with (
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides_path),
                patch.object(catalog_sync, "_CATALOG_OWNER_SECRET_CACHE", None),
            ):
                managed = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
                row = managed["models"][0]
                row["multi_agent_version"] = "v2"
                row["description"] = "forged Official row"
                row["codex_proxy_metadata"].pop(
                    catalog_sync.CATALOG_OWNER_SIGNATURE_KEY,
                    None,
                )
                current_path.write_text(json.dumps(managed), encoding="utf-8")
                with (
                    patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                    patch.object(
                        catalog_sync,
                        "MANAGED_CATALOG_BASELINE_PATH",
                        root / "missing-baseline.json",
                    ),
                ):
                    collected, diagnostics = catalog_sync._collect_catalog_overrides()

            self.assertEqual(collected, {})
            self.assertEqual(diagnostics["reasons"]["invalid_row_identity"], 1)

    def test_catalog_override_rejects_unhashable_multi_agent_values(self):
        identity = ("openai", "official", "gpt-5.6-luna")
        for malformed in ([], {}):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                path = root / "overrides.json"
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "overrides": [
                                {
                                    "provider": identity[0],
                                    "upstream_name": identity[1],
                                    "upstream_model": identity[2],
                                    "fields": {"multi_agent_version": malformed},
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                diagnostics = {}
                self.assertEqual(catalog_sync._load_catalog_override_state(path, diagnostics), {})
                self.assertEqual(diagnostics["reasons"]["invalid_override_fields"], 1)

    def test_managed_baseline_rejects_unhashable_multi_agent_value(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
            baseline.update({"schema_version": 1, "managed_baseline": True})
            baseline["models"][0]["multi_agent_version"] = []
            baseline_path = root / "baseline.json"
            current_path = root / "catalog.json"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            current_path.write_text(json.dumps({"models": baseline["models"]}), encoding="utf-8")
            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline_path),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", root / "overrides.json"),
            ):
                collected, diagnostics = catalog_sync._collect_catalog_overrides()
            self.assertEqual(collected, {})
            self.assertEqual(diagnostics["reasons"]["invalid_baseline"], 1)

    def test_managed_baseline_rejects_numeric_boolean_planner_values(self):
        official = [{"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
            baseline.update({"schema_version": 1, "managed_baseline": True})
            baseline["models"][0]["prefer_websockets"] = 1
            baseline_path = root / "baseline.json"
            current_path = root / "catalog.json"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            current_path.write_text(json.dumps({"models": baseline["models"]}), encoding="utf-8")
            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline_path),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", root / "overrides.json"),
            ):
                collected, diagnostics = catalog_sync._collect_catalog_overrides()
            self.assertEqual(collected, {})
            self.assertEqual(diagnostics["reasons"]["invalid_baseline"], 1)

    def test_markerless_managed_baseline_cannot_seed_an_official_override(self):
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline_model = {
                "slug": identity[2],
                "visibility": "list",
                "prefer_websockets": True,
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v1",
                "use_responses_lite": True,
                "codex_proxy_metadata": {
                    "provider": identity[0],
                    "upstream_name": identity[1],
                    "upstream_model": identity[2],
                },
            }
            current_model = json.loads(json.dumps(baseline_model))
            current_model["multi_agent_version"] = "v2"
            baseline_path = root / "baseline.json"
            current_path = root / "catalog.json"
            baseline_path.write_text(
                json.dumps({"schema_version": 1, "managed_baseline": True, "models": [baseline_model]}),
                encoding="utf-8",
            )
            current_path.write_text(json.dumps({"models": [current_model]}), encoding="utf-8")
            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline_path),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", root / "overrides.json"),
            ):
                collected, diagnostics = catalog_sync._collect_catalog_overrides()
            self.assertEqual(collected, {})
            self.assertEqual(diagnostics["reasons"]["invalid_row_identity"], 1)

    def test_catalog_override_purges_sidecar_when_current_identity_is_missing(self):
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline.json"
            current = root / "catalog.json"
            overrides = root / "overrides.json"
            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "managed_baseline": True,
                        "models": [
                            {
                                "slug": "gpt-5.6-luna",
                                "visibility": "list",
                                "prefer_websockets": True,
                                "tool_mode": "code_mode_only",
                                "multi_agent_version": "v1",
                                "use_responses_lite": True,
                                "codex_proxy_metadata": {
                                    "provider": identity[0],
                                    "upstream_name": identity[1],
                                    "upstream_model": identity[2],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            overrides.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "overrides": [
                            {
                                "provider": identity[0],
                                "upstream_name": identity[1],
                                "upstream_model": identity[2],
                                "fields": {"multi_agent_version": "v2"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            # Same display slug, but a different provider identity.  The old
            # Official sidecar entry must not cross over to this row.
            current.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "volc/gpt-5.6-luna",
                                "visibility": "list",
                                "codex_proxy_metadata": {
                                    "provider": "volc",
                                    "upstream_name": "volcengine",
                                    "upstream_model": "gpt-5.6-luna",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides),
            ):
                collected, diagnostics = catalog_sync._collect_catalog_overrides()

            self.assertEqual(collected, {})
            self.assertEqual(diagnostics["reasons"]["missing_managed_model"], 1)

    def test_catalog_override_reports_malformed_current_catalog_without_using_it_as_edit(self):
        identity = ("openai", "official", "gpt-5.6-luna")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline.json"
            current = root / "catalog.json"
            overrides = root / "overrides.json"
            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "managed_baseline": True,
                        "models": [
                            {
                                "slug": "gpt-5.6-luna",
                                "visibility": "list",
                                "prefer_websockets": True,
                                "tool_mode": "code_mode_only",
                                "multi_agent_version": "v1",
                                "use_responses_lite": True,
                                "codex_proxy_metadata": {
                                    "provider": identity[0],
                                    "upstream_name": identity[1],
                                    "upstream_model": identity[2],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            overrides.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "overrides": [
                            {
                                "provider": identity[0],
                                "upstream_name": identity[1],
                                "upstream_model": identity[2],
                                "fields": {"multi_agent_version": "v2"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            current.write_text("{not json", encoding="utf-8")

            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides),
            ):
                collected, diagnostics = catalog_sync._collect_catalog_overrides()

            self.assertEqual(collected[identity], {"multi_agent_version": "v2"})
            self.assertFalse(diagnostics["current_catalog_valid"])
            self.assertEqual(diagnostics["reasons"]["invalid_catalog"], 1)

            current.write_text(json.dumps({"models": [{}]}), encoding="utf-8")
            with (
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline),
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides),
            ):
                collected, diagnostics = catalog_sync._collect_catalog_overrides()
            self.assertEqual(collected[identity], {"multi_agent_version": "v2"})
            self.assertFalse(diagnostics["current_catalog_valid"])
            self.assertEqual(diagnostics["reasons"]["invalid_catalog"], 1)

    def test_catalog_override_is_removed_when_managed_baseline_catches_up(self):
        identity = ("openai", "official", "gpt-5.6-luna")
        current = {"multi_agent_version": "v2"}
        old_baseline = {"multi_agent_version": "v1"}
        new_baseline = {"multi_agent_version": "v2"}

        self.assertEqual(
            catalog_sync._delta_override_fields(identity, current, old_baseline),
            {"multi_agent_version": "v2"},
        )
        self.assertEqual(
            catalog_sync._delta_override_fields(identity, current, new_baseline),
            {},
        )

    def test_catalog_override_survives_managed_baseline_refresh_publication(self):
        official = [
            {"slug": "gpt-5.6-luna", "display_name": "GPT-5.6-Luna", "visibility": "list"}
        ]
        identity = ("openai", "official", "gpt-5.6-luna")
        managed_v1 = catalog_sync.build_codex_catalog(official, [], self.policy, "0.146.0")
        current = json.loads(json.dumps(managed_v1))
        current["models"][0]["multi_agent_version"] = "v2"
        baseline_v1 = json.loads(json.dumps(managed_v1))
        baseline_v1.update({"schema_version": 1, "managed_baseline": True})
        sidecar_v2 = catalog_sync._write_catalog_override_state(
            {identity: {"multi_agent_version": "v2"}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            current_path = root / "catalog.json"
            baseline_path = root / "baseline.json"
            overrides_path = root / "overrides.json"
            current_path.write_text(json.dumps(current), encoding="utf-8")
            baseline_path.write_text(json.dumps(baseline_v1), encoding="utf-8")
            overrides_path.write_text(json.dumps(sidecar_v2), encoding="utf-8")
            with (
                patch.object(catalog_sync, "GENERATED_CATALOG_PATH", current_path),
                patch.object(catalog_sync, "MANAGED_CATALOG_BASELINE_PATH", baseline_path),
                patch.object(catalog_sync, "CATALOG_OVERRIDES_PATH", overrides_path),
            ):
                collected, diagnostics = catalog_sync._collect_catalog_overrides()
                self.assertEqual(collected[identity], {"multi_agent_version": "v2"})

                pinned_v2 = json.loads(json.dumps(catalog_sync.PINNED_OFFICIAL_CATALOG_METADATA))
                pinned_v2["gpt-5.6-luna"]["multi_agent_version"] = "v2"
                with patch.object(catalog_sync, "PINNED_OFFICIAL_CATALOG_METADATA", pinned_v2):
                    managed_v2 = catalog_sync.build_codex_catalog(
                        official,
                        [],
                        self.policy,
                        "0.146.0",
                    )
                    effective_v2 = catalog_sync._apply_catalog_overrides(
                        json.loads(json.dumps(managed_v2)),
                        collected,
                        diagnostics,
                    )

                self.assertEqual(effective_v2["models"][0]["multi_agent_version"], "v2")
                current_path.write_text(json.dumps(effective_v2), encoding="utf-8")
                baseline_v2 = dict(managed_v2)
                baseline_v2.update({"schema_version": 1, "managed_baseline": True})
                baseline_path.write_text(json.dumps(baseline_v2), encoding="utf-8")
                overrides_path.write_text(json.dumps(catalog_sync._write_catalog_override_state(collected)), encoding="utf-8")

                collected_after, diagnostics_after = catalog_sync._collect_catalog_overrides()
                self.assertEqual(collected_after, {})
                effective_after = catalog_sync._apply_catalog_overrides(
                    json.loads(json.dumps(managed_v2)),
                    collected_after,
                    diagnostics_after,
                )
                self.assertEqual(effective_after["models"][0]["multi_agent_version"], "v2")

    def test_pinned_official_catalog_metadata_rejects_an_incomplete_model_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "official-models.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "models": {
                            "gpt-5.6-sol": {},
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "incomplete model set"):
                catalog_sync.load_pinned_official_catalog_metadata(path)

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

        catalog = build_codex_catalog(
            official,
            [],
            self.policy,
            "0.144.0",
            official_context_signals={
                "gpt-5.6-sol": {
                    "context_window": 400_000,
                    "effective_context_window_percent": 100,
                    "freshness": "fresh",
                    "source": "current_direct_official",
                }
            },
        )
        models = catalog["models"]

        self.assertEqual([model["slug"] for model in models], ["gpt-5.6-sol"])
        self.assertEqual(models[0]["display_name"], "5.6 Sol")
        self.assertEqual(models[0]["context_window"], 400000)
        self.assertEqual(models[0]["multi_agent_version"], "v2")
        self.assertTrue(models[0]["enabled"])

    def test_current_direct_context_signal_bounds_official_catalog_without_touching_third_party(self):
        official = [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6 Sol",
                "context_window": 353_400,
                "max_context_window": 1_050_000,
            }
        ]

        catalog = build_codex_catalog(
            official,
            ["glm-5.2:cloud"],
            self.policy,
            "0.144.0",
            official_context_signals={
                "gpt-5.6-sol": {
                    "context_window": 272_000,
                    "max_context_window": 1_050_000,
                    "effective_context_window_percent": 95,
                    "auto_compact_token_limit": 240_000,
                    "freshness": "fresh",
                    "source": "current_direct_official",
                }
            },
        )

        by_slug = {model["slug"]: model for model in catalog["models"]}
        official_model = by_slug["gpt-5.6-sol"]
        budget = official_model["codex_proxy_metadata"]["official_context_budget"]
        self.assertEqual(official_model["context_window"], 272_000)
        self.assertEqual(official_model["max_context_window"], 272_000)
        self.assertEqual(official_model["effective_context_window_percent"], 95)
        self.assertEqual(budget["effective_context_window"], 258_400)
        self.assertEqual(budget["model_auto_compact_token_limit"], 240_000)
        self.assertEqual(by_slug["glm-5.2"]["context_window"], 1_000_000)

    def test_current_direct_context_signal_uses_snapshot_percent_and_compact_threshold(self):
        catalog = build_codex_catalog(
            [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6 Sol",
                    "context_window": 353_400,
                }
            ],
            [],
            self.policy,
            "0.144.0",
            official_context_signals={
                "gpt-5.6-sol": {
                    "context_window": 300_000,
                    "max_context_window": 900_000,
                    "effective_context_window_percent": 80,
                    "auto_compact_token_limit": 210_000,
                    "freshness": "fresh",
                    "source": "current_direct_official",
                }
            },
        )

        model = catalog["models"][0]
        budget = model["codex_proxy_metadata"]["official_context_budget"]
        self.assertEqual(model["context_window"], 300_000)
        self.assertEqual(model["max_context_window"], 300_000)
        self.assertEqual(model["effective_context_window_percent"], 80)
        self.assertEqual(budget["effective_context_window"], 240_000)
        self.assertEqual(budget["model_auto_compact_token_limit"], 210_000)

    def test_runtime_direct_snapshot_marks_only_fresh_catalog_context_as_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled_seed = root / "bundled" / "openai-plus-ollama-cloud.json"
            runtime_seed = root / "runtime" / "openai-plus-ollama-cloud.json"
            runtime_seed.parent.mkdir(parents=True)
            runtime_seed.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000,
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "context_window": 272_000,
                                "max_context_window": 1_050_000,
                                "effectiveContextWindowPercent": 80,
                                "autoCompactTokenLimit": 200_000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = catalog_sync.load_official_seed_snapshot(
                bundled_seed,
                runtime_path=runtime_seed,
                now_timestamp=1_001,
            )
            signals = catalog_sync.official_context_signals_from_snapshot(snapshot)
            stale_snapshot = catalog_sync.load_official_seed_snapshot(
                bundled_seed,
                runtime_path=runtime_seed,
                now_timestamp=1_000 + catalog_sync.DIRECT_OFFICIAL_CONTEXT_MAX_AGE_SECONDS,
            )

        self.assertEqual(snapshot.context_freshness, "fresh")
        self.assertEqual(signals["gpt-5.6-terra"]["context_window"], 272_000)
        self.assertEqual(signals["gpt-5.6-terra"]["effective_context_window_percent"], 80)
        self.assertEqual(signals["gpt-5.6-terra"]["auto_compact_token_limit"], 200_000)
        self.assertEqual(signals["gpt-5.6-terra"]["freshness"], "fresh")
        self.assertEqual(signals["gpt-5.6-terra"]["source"], "current_direct_official")
        self.assertEqual(stale_snapshot.context_freshness, "stale")
        self.assertNotEqual(stale_snapshot.source, "current_direct_official")

    def test_runtime_direct_snapshot_becomes_stale_at_twelve_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_seed = root / "runtime" / "openai-plus-ollama-cloud.json"
            runtime_seed.parent.mkdir(parents=True)
            runtime_seed.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000,
                        "models": [{"slug": "gpt-5.6-terra", "context_window": 272_000}],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = catalog_sync.load_official_seed_snapshot(
                root / "bundled.json",
                runtime_path=runtime_seed,
                now_timestamp=1_000 + 12 * 60 * 60,
            )

        self.assertEqual(snapshot.context_freshness, "stale")
        self.assertNotEqual(snapshot.source, "current_direct_official")

    def test_codex_0_144_2_seven_model_fixture_without_numeric_context_fails_closed(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_model_list_without_context_fields.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        raw_models = fixture["data"]
        seed_models = [
            {
                **model,
                "slug": model["model"],
                "display_name": model["displayName"],
            }
            for model in raw_models
        ]
        snapshot = catalog_sync.OfficialSeedSnapshot(
            models=seed_models,
            source="current_direct_official",
            context_freshness="fresh",
        )
        signals = catalog_sync.official_context_signals_from_snapshot(snapshot)
        catalog = build_codex_catalog(
            snapshot.models,
            [],
            catalog_sync.load_policy(catalog_sync.POLICY_PATH),
            "0.144.2",
            official_context_signals=signals,
        )
        expected_slugs = [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex-spark",
        ]
        numeric_context_fields = (
            "context_window",
            "max_context_window",
            "effective_context_window_percent",
            "auto_compact_token_limit",
        )

        self.assertEqual([model["slug"] for model in catalog["models"]], expected_slugs)
        for slug in expected_slugs:
            with self.subTest(slug=slug):
                self.assertTrue(all(signals[slug][field] is None for field in numeric_context_fields))
                model = next(model for model in catalog["models"] if model["slug"] == slug)
                self.assertTrue(all(field not in model for field in numeric_context_fields))
                self.assertEqual(
                    model["codex_proxy_metadata"]["official_context_budget"],
                    {"source": "current_direct_official", "freshness": "fresh"},
                )

    def test_fresh_direct_cache_authority_recovers_numeric_budget_from_no_numeric_model_list(self):
        model_list_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_model_list_without_context_fields.json"
        )
        cache_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_direct_models_cache.json"
        )
        raw_models = json.loads(model_list_path.read_text(encoding="utf-8"))["data"]
        cache_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(
            next(model for model in cache_payload["models"] if model["slug"] == "codex-auto-review"),
            {"slug": "codex-auto-review"},
        )
        snapshot = catalog_sync.OfficialSeedSnapshot(
            models=[
                {
                    **model,
                    "slug": model["model"],
                    "display_name": model["displayName"],
                }
                for model in raw_models
            ],
            source="current_direct_official",
            context_freshness="fresh",
        )
        cache_timestamp = catalog_sync._catalog_fetched_at_timestamp(cache_payload["fetched_at"])
        self.assertIsNotNone(cache_timestamp)

        with tempfile.TemporaryDirectory() as tmp:
            runtime_cache = Path(tmp) / "models_cache.json"
            runtime_cache.write_text(json.dumps(cache_payload), encoding="utf-8")
            authority = catalog_sync.load_fresh_direct_official_cache_authority(
                snapshot,
                runtime_cache,
                now_timestamp=cache_timestamp + 1,
            )

        signals = catalog_sync.official_context_signals_from_snapshot(
            snapshot,
            direct_cache_authority=authority,
        )
        catalog = build_codex_catalog(
            snapshot.models,
            ["glm-5.2:cloud"],
            self.policy,
            "0.144.2",
            official_context_signals=signals,
        )
        by_slug = {model["slug"]: model for model in catalog["models"]}
        serialized_catalog = json.dumps(catalog, sort_keys=True)
        self.assertNotIn("sanitized-direct-cache-etag", serialized_catalog)
        self.assertNotIn("sanitized-config-marker", serialized_catalog)
        self.assertNotIn("models_cache.json", serialized_catalog)
        self.assertNotIn("codex-auto-review", serialized_catalog)

        expected_context_windows = {
            "gpt-5.6-sol": 272_000,
            "gpt-5.6-terra": 272_000,
            "gpt-5.6-luna": 272_000,
            "gpt-5.5": 272_000,
            "gpt-5.4": 272_000,
            "gpt-5.4-mini": 272_000,
            "gpt-5.3-codex-spark": 128_000,
        }
        self.assertEqual(set(authority.context_by_slug), set(expected_context_windows))
        for slug, context_window in expected_context_windows.items():
            with self.subTest(slug=slug):
                model = by_slug[slug]
                budget = model["codex_proxy_metadata"]["official_context_budget"]
                self.assertEqual(model["context_window"], context_window)
                self.assertEqual(model["max_context_window"], context_window)
                self.assertEqual(model["effective_context_window_percent"], 95)
                self.assertEqual(
                    budget["source"],
                    "fresh_direct_official_cache_authority",
                )
                self.assertEqual(budget["freshness"], "fresh")
                self.assertLessEqual(budget["effective_context_window"], context_window * 95 // 100)
                self.assertLessEqual(
                    budget["model_auto_compact_token_limit"],
                    min(context_window * 90 // 100, context_window * 95 // 100),
                )
                self.assertLess(budget["model_auto_compact_token_limit"], 249_433)
                serialized_budget = json.dumps(budget, sort_keys=True)
                self.assertNotIn("sanitized-direct-cache-etag", serialized_budget)
                self.assertNotIn("sanitized-config-marker", serialized_budget)
                self.assertNotIn("models_cache.json", serialized_budget)

        self.assertEqual(by_slug["glm-5.2"]["context_window"], 1_000_000)
        self.assertNotIn("353400", serialized_catalog)

    def test_sync_catalog_reads_target_only_direct_cache_before_same_attempt_publication(self):
        model_list_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_model_list_without_context_fields.json"
        )
        direct_cache_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_direct_models_cache.json"
        )
        raw_models = json.loads(model_list_path.read_text(encoding="utf-8"))["data"]
        direct_cache = json.loads(direct_cache_path.read_text(encoding="utf-8"))
        direct_cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
        direct_cache["etag"] = "target-cache-private-etag"
        for model in direct_cache["models"]:
            if model["slug"].startswith("gpt-"):
                model["comp_hash"] = f"target-cache-private-hash-{model['slug']}"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_home = root / "runtime"
            target_home = root / "target-home-must-not-leak"
            runtime_seed = runtime_home / "model-catalogs" / "openai-plus-ollama-cloud.json"
            target_cache = target_home / "models_cache.json"
            runtime_seed.parent.mkdir(parents=True)
            runtime_seed.write_text(
                json.dumps(
                    {
                        "fetched_at": direct_cache["fetched_at"],
                        "client_version": "0.144.2",
                        "models": [
                            {
                                **model,
                                "slug": model["model"],
                                "display_name": model["displayName"],
                            }
                            for model in raw_models
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse((runtime_home / "models_cache.json").exists())
            self.assertFalse(target_cache.exists())
            target_cache.parent.mkdir(parents=True)
            target_cache.write_text(json.dumps(direct_cache), encoding="utf-8")

            try:
                with patch.dict(
                    "os.environ",
                    {
                        "CODEX_HOME": str(runtime_home),
                        "CODEXHUB_CODEX_TARGET_HOME": str(target_home),
                    },
                    clear=False,
                ):
                    importlib.reload(catalog_sync)
                    self.assertEqual(catalog_sync.RUNTIME_CODEX_DIR, runtime_home)
                    self.assertEqual(
                        catalog_sync.DIRECT_OFFICIAL_MODELS_CACHE_PATH,
                        target_cache,
                    )
                    with (
                        patch.object(catalog_sync, "catalog_cache_is_fresh", return_value=False),
                        patch.object(catalog_sync, "load_include_official_models", return_value=True),
                        patch.object(catalog_sync, "load_official_model_sort_order", return_value=[]),
                        patch.object(catalog_sync, "load_official_disabled_models", return_value=[]),
                        patch.object(catalog_sync, "load_fallback_catalog_models", return_value=[]),
                        patch.object(catalog_sync, "read_client_version", return_value="0.144.2"),
                        patch.object(
                            catalog_sync,
                            "discover_ollama_ids",
                            return_value=([], "test", "ok", ""),
                        ),
                        patch.object(catalog_sync, "load_providers", return_value=[]),
                        patch.object(
                            catalog_sync,
                            "catalog_visible_ollama_cloud_models",
                            return_value=(False, []),
                        ),
                        patch.object(catalog_sync, "catalog_visible_external_models", return_value=[]),
                        patch.object(
                            catalog_sync,
                            "discover_ollama_model_metadata",
                            return_value=({}, ""),
                        ),
                        patch.object(catalog_sync, "load_previous_visible_models", return_value=set()),
                    ):
                        state = catalog_sync.sync_catalog()
            finally:
                importlib.reload(catalog_sync)

            generated_catalog = runtime_home / "model-catalogs" / "codexhub-model-catalog.json"
            generated_state = runtime_home / "model-catalogs" / "codex-proxy-state.json"
            self.assertTrue(generated_catalog.is_file())
            self.assertTrue(generated_state.is_file())
            self.assertFalse(
                (target_home / "model-catalogs" / "codexhub-model-catalog.json").exists()
            )
            self.assertFalse((target_home / "model-catalogs" / "codex-proxy-state.json").exists())
            self.assertEqual(state["visible_models"], [model["model"] for model in raw_models])

            serialized_catalog = generated_catalog.read_text(encoding="utf-8")
            generated = json.loads(serialized_catalog)
            terra = next(model for model in generated["models"] if model["slug"] == "gpt-5.6-terra")
            budget = terra["codex_proxy_metadata"]["official_context_budget"]
            self.assertEqual(terra["context_window"], 272_000)
            self.assertEqual(budget["source"], "fresh_direct_official_cache_authority")
            self.assertEqual(budget["freshness"], "fresh")
            self.assertLessEqual(budget["effective_context_window"], 258_400)
            self.assertLessEqual(budget["model_auto_compact_token_limit"], 244_800)
            serialized_state = generated_state.read_text(encoding="utf-8")
            for generated_artifact in (serialized_catalog, serialized_state):
                self.assertNotIn("target-cache-private-etag", generated_artifact)
                self.assertNotIn("target-cache-private-hash", generated_artifact)
                self.assertNotIn(target_home.name, generated_artifact)
                self.assertNotIn(str(target_home), generated_artifact)
                self.assertNotIn("models_cache.json", generated_artifact)

    def test_direct_cache_path_uses_runtime_home_when_target_home_is_not_provided(self):
        model_list_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_model_list_without_context_fields.json"
        )
        direct_cache_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_direct_models_cache.json"
        )
        raw_models = json.loads(model_list_path.read_text(encoding="utf-8"))["data"]
        direct_cache = json.loads(direct_cache_path.read_text(encoding="utf-8"))
        direct_cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            runtime_home = Path(tmp) / "runtime"
            runtime_cache = runtime_home / "models_cache.json"
            runtime_home.mkdir(parents=True)
            runtime_cache.write_text(json.dumps(direct_cache), encoding="utf-8")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "CODEX_HOME": str(runtime_home),
                        "CODEXHUB_CODEX_TARGET_HOME": "",
                    },
                    clear=False,
                ):
                    importlib.reload(catalog_sync)
                    self.assertEqual(catalog_sync.RUNTIME_CODEX_DIR, runtime_home)
                    self.assertEqual(
                        catalog_sync.DIRECT_OFFICIAL_MODELS_CACHE_PATH,
                        runtime_cache,
                    )
                    authority = catalog_sync.load_fresh_direct_official_cache_authority(
                        catalog_sync.OfficialSeedSnapshot(
                            models=[
                                {
                                    **model,
                                    "slug": model["model"],
                                    "display_name": model["displayName"],
                                }
                                for model in raw_models
                            ],
                            source="current_direct_official",
                            context_freshness="fresh",
                        )
                    )
                    self.assertEqual(
                        authority.context_by_slug["gpt-5.6-terra"]["context_window"],
                        272_000,
                    )
            finally:
                importlib.reload(catalog_sync)

    def test_direct_cache_authority_rejects_invalid_provenance_identity_and_numeric_evidence(self):
        model_list_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_model_list_without_context_fields.json"
        )
        cache_path = (
            Path(__file__).parent
            / "fixtures"
            / "codex_0_144_2_direct_models_cache.json"
        )
        raw_models = json.loads(model_list_path.read_text(encoding="utf-8"))["data"]
        fresh_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        snapshot = catalog_sync.OfficialSeedSnapshot(
            models=[
                {
                    **model,
                    "slug": model["model"],
                    "display_name": model["displayName"],
                }
                for model in raw_models
            ],
            source="current_direct_official",
            context_freshness="fresh",
        )
        cache_timestamp = catalog_sync._catalog_fetched_at_timestamp(fresh_cache["fetched_at"])
        self.assertIsNotNone(cache_timestamp)

        cases: list[tuple[str, object, float, str]] = []
        stale_cache = json.loads(json.dumps(fresh_cache))
        cases.append(
            (
                "stale",
                stale_cache,
                cache_timestamp + catalog_sync.DIRECT_OFFICIAL_CONTEXT_MAX_AGE_SECONDS,
                "stale",
            )
        )
        for label, key in (("missing_etag", "etag"), ("missing_client_version", "client_version")):
            payload = json.loads(json.dumps(fresh_cache))
            payload.pop(key)
            cases.append((label, payload, cache_timestamp + 1, "missing"))
        missing_all_numeric = json.loads(json.dumps(fresh_cache))
        missing_numeric_model = next(
            model for model in missing_all_numeric["models"] if model["slug"] == "gpt-5.6-terra"
        )
        for key in (
            "context_window",
            "max_context_window",
            "effective_context_window_percent",
        ):
            missing_numeric_model.pop(key)
        cases.append(("missing_all_numeric", missing_all_numeric, cache_timestamp + 1, "missing"))
        missing_context_window = json.loads(json.dumps(fresh_cache))
        next(
            model for model in missing_context_window["models"] if model["slug"] == "gpt-5.6-terra"
        ).pop("context_window")
        cases.append(("missing_context_window", missing_context_window, cache_timestamp + 1, "contradictory"))
        missing_marker = json.loads(json.dumps(fresh_cache))
        next(model for model in missing_marker["models"] if model["slug"] == "gpt-5.6-terra").pop(
            "comp_hash"
        )
        cases.append(("missing_comp_hash", missing_marker, cache_timestamp + 1, "missing"))
        mismatched_identity = json.loads(json.dumps(fresh_cache))
        next(
            model for model in mismatched_identity["models"] if model["slug"] == "gpt-5.6-terra"
        )["slug"] = "gpt-cache-mismatch"
        cases.append(("mismatched_model_id", mismatched_identity, cache_timestamp + 1, "contradictory"))
        mixed_gpt_identity = json.loads(json.dumps(fresh_cache))
        next(
            model for model in mixed_gpt_identity["models"] if model["slug"] == "gpt-5.6-terra"
        )["model"] = "gpt-5.6-luna"
        cases.append(("mixed_gpt_identity", mixed_gpt_identity, cache_timestamp + 1, "contradictory"))
        contradictory_numeric = json.loads(json.dumps(fresh_cache))
        next(
            model for model in contradictory_numeric["models"] if model["slug"] == "gpt-5.6-terra"
        )["max_context_window"] = 271_999
        cases.append(("contradictory_numeric", contradictory_numeric, cache_timestamp + 1, "contradictory"))
        duplicate_identity = json.loads(json.dumps(fresh_cache))
        duplicate_identity["models"].append(
            dict(next(model for model in duplicate_identity["models"] if model["slug"] == "gpt-5.6-terra"))
        )
        cases.append(("duplicate_model_id", duplicate_identity, cache_timestamp + 1, "contradictory"))
        cases.append(("unparseable", "{not json", cache_timestamp + 1, "missing"))

        for label, payload, now_timestamp, expected_freshness in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                runtime_cache = Path(tmp) / "models_cache.json"
                text = payload if isinstance(payload, str) else json.dumps(payload)
                runtime_cache.write_text(text, encoding="utf-8")
                authority = catalog_sync.load_fresh_direct_official_cache_authority(
                    snapshot,
                    runtime_cache,
                    now_timestamp=now_timestamp,
                )
                signals = catalog_sync.official_context_signals_from_snapshot(
                    snapshot,
                    direct_cache_authority=authority,
                )
                catalog = build_codex_catalog(
                    snapshot.models,
                    [],
                    self.policy,
                    "0.144.2",
                    official_context_signals=signals,
                )
                terra = next(model for model in catalog["models"] if model["slug"] == "gpt-5.6-terra")

                self.assertEqual(authority.context_by_slug, {})
                self.assertEqual(authority.freshness, expected_freshness)
                self.assertNotIn("context_window", terra)
                self.assertEqual(
                    terra["codex_proxy_metadata"]["official_context_budget"],
                    {"source": "direct_official_cache", "freshness": expected_freshness},
                )

    def test_previous_official_budget_requires_proven_safe_resolution_provenance(self):
        def budget(source: str, freshness: str, context_window: int) -> dict[str, object]:
            return {
                "source": source,
                "freshness": freshness,
                "model_context_window": context_window,
                "effective_context_window_percent": 95,
                "effective_context_window": context_window * 95 // 100,
                "model_auto_compact_token_limit": context_window * 90 // 100,
            }

        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": budget(
                                        "fresh_direct_official_cache_authority",
                                        "fresh",
                                        272_000,
                                    ),
                                },
                            },
                            {
                                "slug": "gpt-5.6-sol",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": budget(
                                        "current_direct_official",
                                        "stale",
                                        353_400,
                                    ),
                                },
                            },
                            {
                                "slug": "gpt-5.6-luna",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": budget(
                                        "degraded_last_known_official",
                                        "stale",
                                        272_000,
                                    ),
                                },
                            },
                            {
                                "slug": "gpt-5.5",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": budget(
                                        "stale_probe_cache",
                                        "fresh",
                                        353_400,
                                    ),
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            budgets = catalog_sync.load_previous_official_context_budgets(catalog_path)

        self.assertEqual(set(budgets), {"gpt-5.6-terra", "gpt-5.6-luna"})
        self.assertEqual(budgets["gpt-5.6-terra"]["model_context_window"], 272_000)
        self.assertNotIn("gpt-5.6-sol", budgets)
        self.assertNotIn("gpt-5.5", budgets)

    def test_stale_direct_snapshot_can_only_reuse_a_previously_resolved_budget(self):
        snapshot = catalog_sync.OfficialSeedSnapshot(
            models=[{"slug": "gpt-5.5", "context_window": 400_000}],
            source="last_known_direct_official",
            context_freshness="stale",
        )
        previous = {
            "gpt-5.5": {
                "model_context_window": 300_000,
                "effective_context_window_percent": 80,
                "model_auto_compact_token_limit": 210_000,
            }
        }
        signals = catalog_sync.official_context_signals_from_snapshot(snapshot, previous)
        catalog = build_codex_catalog(
            snapshot.models,
            [],
            self.policy,
            "0.144.0",
            official_context_signals=signals,
        )
        model = next(model for model in catalog["models"] if model["slug"] == "gpt-5.5")
        budget = model["codex_proxy_metadata"]["official_context_budget"]

        self.assertEqual(model["context_window"], 300_000)
        self.assertEqual(budget["source"], "degraded_last_known_official")
        self.assertEqual(budget["model_auto_compact_token_limit"], 210_000)

        no_prior_catalog = build_codex_catalog(
            snapshot.models,
            [],
            self.policy,
            "0.144.0",
            official_context_signals=catalog_sync.official_context_signals_from_snapshot(snapshot),
        )
        no_prior_model = next(
            model for model in no_prior_catalog["models"] if model["slug"] == "gpt-5.5"
        )
        self.assertNotIn("context_window", no_prior_model)

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
        self.assertEqual(by_slug["gpt-5.6-sol"]["codex_proxy_metadata"]["provider"], "openai")
        self.assertEqual(by_slug["gpt-5.6-sol"]["codex_proxy_metadata"]["upstream_name"], "official")
        self.assertEqual(by_slug["gpt-5.6-sol"]["codex_proxy_metadata"]["upstream_model"], "gpt-5.6-sol")
        self.assertNotIn("context_window", by_slug["gpt-5.6-sol"])
        self.assertEqual(
            by_slug["gpt-5.6-sol"]["codex_proxy_metadata"]["official_context_budget"],
            {"source": "missing", "freshness": "missing"},
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

        self.assertNotIn("context_window", by_slug["gpt-5.5"])
        self.assertNotIn("max_context_window", by_slug["gpt-5.5"])
        self.assertNotIn("context_window", catalog_sync.OFFICIAL_MODEL_DEFAULTS["gpt-5.5-fast"])
        self.assertNotIn("max_context_window", catalog_sync.OFFICIAL_MODEL_DEFAULTS["gpt-5.5-fast"])
        self.assertEqual(by_slug["gpt-5.5"]["additional_speed_tiers"], ["fast"])
        self.assertEqual(by_slug["gpt-5.5"]["service_tiers"][0]["id"], "priority")
        self.assertEqual(by_slug["gpt-5.5"]["default_reasoning_level"], "medium")
        self.assertNotIn("gpt-5.5-fast", by_slug)
        self.assertNotIn("context_window", by_slug["gpt-5.4"])
        self.assertEqual(by_slug["gpt-5.4"]["additional_speed_tiers"], ["fast"])
        self.assertNotIn("gpt-5.4-fast", by_slug)
        self.assertNotIn("context_window", by_slug["gpt-5.4-mini"])
        self.assertEqual(by_slug["gpt-5.4-mini"]["additional_speed_tiers"], [])
        self.assertNotIn("context_window", by_slug["gpt-5.3-codex-spark"])
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

    def test_policy_fallback_lists_current_official_bindings_without_a_runtime_seed(self):
        policy = catalog_sync.load_policy(catalog_sync.POLICY_PATH)

        catalog = build_codex_catalog([], [], policy, "0.144.0")
        by_slug = {model["slug"]: model for model in catalog["models"]}

        self.assertEqual(
            [model_id for model_id in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna") if model_id in by_slug],
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        )
        self.assertEqual(by_slug["gpt-5.6-terra"]["display_name"], "5.6 Terra")
        self.assertNotIn("context_window", by_slug["gpt-5.6-terra"])
        self.assertNotIn("max_context_window", by_slug["gpt-5.6-terra"])
        self.assertIn(
            "max",
            [entry["effort"] for entry in by_slug["gpt-5.6-terra"]["supported_reasoning_levels"]],
        )

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

