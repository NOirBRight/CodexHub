from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from providers_config import (
    ModelConfig,
    ProviderConfig,
    build_external_model_index,
    load_providers,
    resolve_external_model_alias,
    save_providers,
)


class ProvidersConfigTests(unittest.TestCase):
    def test_build_external_model_index_emits_default_like_external_provider_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.com/v1"
api_key = "{env:OLLAMA_API_KEY}"
display_prefix = "Ollama"
sort_order = 1
enabled = true

  [[providers.models]]
  id = "minimax-m3"
  context_window = 512000
  max_output_tokens = 524288
  sort_order = 1
  enabled = true

[[providers]]
id = "volc"
name = "Volcengine"
base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"
api_key = "{env:VOLCENGINE_API_KEY}"
display_prefix = "Volc"
sort_order = 2
enabled = true

  [[providers.models]]
  id = "glm-5.2"
  context_window = 1024000
  max_output_tokens = 8192
  sort_order = 1
  enabled = true

[[providers]]
id = "minimax-cn"
name = "MiniMax.cn"
base_url = "https://api.minimaxi.com/v1"
api_key = "literal-minimax-secret"
display_prefix = "MiniMax.cn"
sort_order = 3
enabled = true

  [[providers.models]]
  id = "minimax-m3"
  context_window = 1000000
  max_output_tokens = 524288
  sort_order = 1
  enabled = true
""".lstrip(),
                encoding="utf-8",
            )
            providers = load_providers(path)

        with patch.dict("os.environ", {"VOLCENGINE_API_KEY": "volc-secret", "OLLAMA_API_KEY": "ollama-secret"}):
            index = build_external_model_index(providers)

        self.assertEqual(sorted(index), ["minimax-cn/minimax-m3", "volc/glm-5.2"])

        volc = index["volc/glm-5.2"]
        self.assertEqual(volc["alias"], "volc/glm-5.2")
        self.assertEqual(volc["provider_alias"], "volc")
        self.assertEqual(volc["upstream_name"], "volcengine")
        self.assertEqual(volc["display_prefix"], "Volc")
        self.assertEqual(volc["base_url"], "https://ark.cn-beijing.volces.com/api/coding/v3")
        self.assertEqual(volc["api_key"], "volc-secret")
        self.assertEqual(volc["upstream_model"], "glm-5.2")
        self.assertEqual(volc["context_window"], 1024000)
        self.assertEqual(volc["max_output_tokens"], 8192)
        self.assertEqual(volc["input_modalities"], ("text",))
        self.assertEqual(volc["context_source"], "providers_toml")
        self.assertEqual(volc["max_output_source"], "providers_toml")
        self.assertEqual(volc["priority_base"], 200)

        minimax = index["minimax-cn/minimax-m3"]
        self.assertEqual(minimax["upstream_name"], "minimax_cn")
        self.assertEqual(minimax["display_prefix"], "MiniMax.cn")
        self.assertEqual(minimax["base_url"], "https://api.minimaxi.com/v1")
        self.assertEqual(minimax["api_key"], "literal-minimax-secret")
        self.assertEqual(minimax["upstream_model"], "minimax-m3")
        self.assertEqual(minimax["context_window"], 1000000)
        self.assertEqual(minimax["max_output_tokens"], 524288)
        self.assertEqual(minimax["priority_base"], 300)

    def test_build_external_model_index_skips_disabled_providers_and_models(self):
        providers = [
            ProviderConfig(
                id="disabled-provider",
                name="Disabled Provider",
                base_url="https://disabled.example/v1",
                api_key="disabled-secret",
                enabled=False,
                models=[ModelConfig(id="enabled-model")],
            ),
            ProviderConfig(
                id="enabled-provider",
                name="Enabled Provider",
                base_url="https://enabled.example/v1",
                api_key="enabled-secret",
                models=[
                    ModelConfig(id="disabled-model", enabled=False),
                    ModelConfig(id="enabled-model"),
                ],
            ),
        ]

        index = build_external_model_index(providers)

        self.assertEqual(sorted(index), ["enabled-provider/enabled-model"])

    def test_build_external_model_index_skips_missing_env_keys_and_resolves_present_env_keys(self):
        providers = [
            ProviderConfig(
                id="missing-key",
                name="Missing Key",
                base_url="https://missing.example/v1",
                api_key="{env:PROVIDERS_CONFIG_MISSING_EXTERNAL_KEY}",
                models=[ModelConfig(id="model")],
            ),
            ProviderConfig(
                id="present-key",
                name="Present Key",
                base_url="https://present.example/v1",
                api_key="{env:PROVIDERS_CONFIG_PRESENT_EXTERNAL_KEY}",
                models=[ModelConfig(id="model")],
            ),
        ]

        with patch.dict(
            "os.environ",
            {"PROVIDERS_CONFIG_PRESENT_EXTERNAL_KEY": "present-secret"},
            clear=True,
        ):
            index = build_external_model_index(providers)

        self.assertEqual(sorted(index), ["present-key/model"])
        self.assertEqual(index["present-key/model"]["api_key"], "present-secret")

    def test_build_external_model_index_excludes_ollama_cloud_provider(self):
        providers = [
            ProviderConfig(
                id="ollama-cloud",
                name="Ollama Cloud",
                base_url="https://ollama.com/v1",
                api_key="ollama-secret",
                models=[ModelConfig(id="minimax-m3")],
            )
        ]

        self.assertEqual(build_external_model_index(providers), {})

    def test_resolve_external_model_alias_uses_canonical_aliases_and_returns_none_for_unknown_or_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "volc"
name = "Volcengine"
base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"
api_key = "{env:VOLCENGINE_API_KEY}"
display_prefix = "Volc"
sort_order = 2
enabled = true

  [[providers.models]]
  id = "glm-5.2"
  context_window = 1024000
  max_output_tokens = 8192
  sort_order = 1
  enabled = true

  [[providers.models]]
  id = "disabled-model"
  enabled = false
""".lstrip(),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"VOLCENGINE_API_KEY": "volc-secret"}, clear=False):
                resolved = resolve_external_model_alias("  volc/glm-5.2:cloud  ", providers_path=path)
                disabled = resolve_external_model_alias("volc/disabled-model", providers_path=path)
                unknown = resolve_external_model_alias("volc/unknown-model", providers_path=path)

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["alias"], "volc/glm-5.2")
        self.assertEqual(resolved["upstream_name"], "volcengine")
        self.assertEqual(resolved["base_url"], "https://ark.cn-beijing.volces.com/api/coding/v3")
        self.assertEqual(resolved["api_key"], "volc-secret")
        self.assertEqual(resolved["upstream_model"], "glm-5.2")
        self.assertEqual(resolved["context_window"], 1024000)
        self.assertEqual(resolved["max_output_tokens"], 8192)
        self.assertEqual(resolved["display_prefix"], "Volc")
        self.assertIsNone(disabled)
        self.assertIsNone(unknown)

    def test_missing_file_returns_empty_provider_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.toml"

            self.assertEqual(load_providers(path), [])

    def test_default_config_uses_provider_ids_that_match_model_slug_prefixes(self):
        providers = load_providers()

        self.assertEqual([provider.id for provider in providers], ["ollama-cloud", "volc", "minimax-cn"])

    def test_default_config_external_aliases_exclude_volc_minimax_m3_by_default(self):
        with patch.dict(
            "os.environ",
            {
                "OLLAMA_API_KEY": "ollama-secret",
                "VOLCENGINE_API_KEY": "volc-secret",
                "MINIMAX_API_KEY": "minimax-secret",
            },
            clear=True,
        ):
            index = build_external_model_index(load_providers())
            volc_minimax = resolve_external_model_alias("volc/minimax-m3")

        self.assertEqual(sorted(index), ["minimax-cn/minimax-m3", "volc/glm-5.2"])
        self.assertNotIn("volc/minimax-m3", index)
        self.assertIsNone(volc_minimax)

    def test_load_parses_providers_models_and_sorts_by_sort_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "later"
name = "Later Provider"
base_url = "https://later.example/v1"
api_key = "literal-secret"
sort_order = 20

  [[providers.models]]
  id = "beta"
  display_name = "Beta Model"
  context_window = 20
  max_output_tokens = 10
  sort_order = 2
  enabled = false

  [[providers.models]]
  id = "alpha"
  sort_order = 1

[[providers]]
id = "first"
name = "First Provider"
base_url = "https://first.example/v1"
api_key = "{env:FIRST_PROVIDER_API_KEY}"
display_prefix = "First"
sort_order = 10
enabled = false

[[providers]]
id = "tie"
name = "Tie Provider"
base_url = "https://tie.example/v1"
api_key = ""
sort_order = 20
""".lstrip(),
                encoding="utf-8",
            )

            providers = load_providers(path)

        self.assertEqual([provider.id for provider in providers], ["first", "later", "tie"])
        self.assertFalse(providers[0].enabled)
        self.assertEqual(providers[0].display_prefix, "First")
        self.assertEqual([model.id for model in providers[1].models], ["alpha", "beta"])
        self.assertIsNone(providers[1].models[0].display_name)
        self.assertEqual(providers[1].models[1].display_name, "Beta Model")
        self.assertEqual(providers[1].models[1].context_window, 20)
        self.assertEqual(providers[1].models[1].max_output_tokens, 10)
        self.assertFalse(providers[1].models[1].enabled)

    def test_resolved_api_key_handles_env_placeholders_literals_and_empty_values(self):
        env_provider = ProviderConfig(
            id="env",
            name="Env Provider",
            base_url="https://env.example/v1",
            api_key="{env:PROVIDERS_CONFIG_TEST_KEY}",
        )
        literal_provider = ProviderConfig(
            id="literal",
            name="Literal Provider",
            base_url="https://literal.example/v1",
            api_key="literal-secret",
        )
        missing_env_provider = ProviderConfig(
            id="missing-env",
            name="Missing Env Provider",
            base_url="https://missing.example/v1",
            api_key="{env:PROVIDERS_CONFIG_MISSING_KEY}",
        )
        empty_provider = ProviderConfig(
            id="empty",
            name="Empty Provider",
            base_url="https://empty.example/v1",
            api_key="",
        )

        with patch.dict("os.environ", {"PROVIDERS_CONFIG_TEST_KEY": "env-secret"}, clear=False):
            self.assertEqual(env_provider.resolved_api_key(), "env-secret")

        self.assertEqual(literal_provider.resolved_api_key(), "literal-secret")
        self.assertIsNone(missing_env_provider.resolved_api_key())
        self.assertIsNone(empty_provider.resolved_api_key())

    def test_resolved_api_key_keeps_partial_env_placeholders_as_literals(self):
        provider = ProviderConfig(
            id="partial",
            name="Partial Provider",
            base_url="https://partial.example/v1",
            api_key="prefix-{env:PROVIDERS_CONFIG_TEST_KEY}",
        )

        with patch.dict("os.environ", {"PROVIDERS_CONFIG_TEST_KEY": "env-secret"}, clear=False):
            self.assertEqual(provider.resolved_api_key(), "prefix-{env:PROVIDERS_CONFIG_TEST_KEY}")

    def test_load_rejects_providers_table_that_is_not_an_array(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[providers]
id = "not-an-array"
""".lstrip(),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_providers(path)

    def test_load_rejects_models_table_that_is_not_an_array(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "provider"
name = "Provider"
base_url = "https://provider.example/v1"
api_key = "literal-secret"

  [providers.models]
  id = "not-an-array"
""".lstrip(),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_providers(path)

    def test_load_coerces_simple_string_values_without_inventing_numeric_limits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "provider"
name = "Provider"
base_url = "https://provider.example/v1"
api_key = "{env:PROVIDER_KEY}"
sort_order = "7"
enabled = "0"

  [[providers.models]]
  id = "model"
  context_window = "512000"
  max_output_tokens = "not-a-number"
  enabled = "false"
""".lstrip(),
                encoding="utf-8",
            )

            providers = load_providers(path)

        self.assertEqual(providers[0].sort_order, 7)
        self.assertFalse(providers[0].enabled)
        self.assertEqual(providers[0].models[0].context_window, 512000)
        self.assertIsNone(providers[0].models[0].max_output_tokens)
        self.assertFalse(providers[0].models[0].enabled)

    def test_save_providers_roundtrips_configured_fields_without_resolving_secrets(self):
        providers = [
            ProviderConfig(
                id="provider",
                name="Provider",
                base_url="https://provider.example/v1",
                api_key="{env:ROUNDTRIP_PROVIDER_KEY}",
                display_prefix="Provider",
                sort_order=5,
                enabled=False,
                models=[
                    ModelConfig(
                        id="disabled-model",
                        display_name="Disabled Model",
                        context_window=128000,
                        max_output_tokens=8192,
                        sort_order=2,
                        enabled=False,
                    ),
                    ModelConfig(id="enabled-model", sort_order=1),
                ],
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "providers.toml"
            with patch.dict("os.environ", {"ROUNDTRIP_PROVIDER_KEY": "must-not-be-written"}, clear=False):
                save_providers(providers, path)

            loaded = load_providers(path)
            raw_toml = path.read_text(encoding="utf-8")

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].api_key, "{env:ROUNDTRIP_PROVIDER_KEY}")
        self.assertFalse(loaded[0].enabled)
        self.assertEqual([model.id for model in loaded[0].models], ["enabled-model", "disabled-model"])
        self.assertFalse(loaded[0].models[1].enabled)
        self.assertIn('api_key = "{env:ROUNDTRIP_PROVIDER_KEY}"', raw_toml)
        self.assertNotIn("must-not-be-written", raw_toml)


if __name__ == "__main__":
    unittest.main()
