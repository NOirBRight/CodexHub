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
                "openai/gpt-5.5",
                "openai/gpt-5.4",
                "openai/gpt-5.4-mini",
                "openai/gpt-5.3-codex-spark",
                "glm-5.2",
                "kimi-k2.7-code",
            ],
        )
        self.assertEqual(catalog["models"][3]["display_name"], "OpenAI GPT-5.3-Codex-Spark")
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
                "openai/gpt-5.5",
                "openai/gpt-5.4",
                "openai/gpt-5.4-mini",
                "openai/gpt-5.3-codex-spark",
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
            [model["priority"] for model in catalog["models"][4:]],
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
        ]

        catalog = build_codex_catalog(official, [], self.policy, "0.142.0")
        by_slug = {model["slug"]: model for model in catalog["models"]}

        self.assertEqual(by_slug["openai/gpt-5.5"]["display_name"], "OpenAI GPT-5.5")
        self.assertEqual(by_slug["openai/gpt-5.5"]["additional_speed_tiers"], ["fast"])
        self.assertEqual(by_slug["openai/gpt-5.5"]["codex_proxy_metadata"]["upstream_model"], "gpt-5.5")
        self.assertEqual(by_slug["openai/gpt-5.4"]["service_tiers"][0]["id"], "priority")
        self.assertNotIn("additional_speed_tiers", by_slug["openai/gpt-5.4-mini"])

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
                "priority_base": 200,
                "context_window": 1024000,
                "max_output_tokens": 4096,
                "input_modalities": ("text",),
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
        self.assertEqual(by_slug["volc/minimax-m3"]["priority"], 201)
        self.assertEqual(by_slug["volc/minimax-m3"]["input_modalities"], ["text", "image"])
        self.assertEqual(by_slug["volc/glm-5.2"]["codex_proxy_metadata"]["provider"], "volc")
        self.assertEqual(by_slug["volc/glm-5.2"]["codex_proxy_metadata"]["upstream_model"], "glm-5.2")
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

    def test_write_json_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "missing" / "model-catalogs" / "state.json"

            catalog_sync.write_json(target, {"ok": True})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})

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


if __name__ == "__main__":
    unittest.main()

