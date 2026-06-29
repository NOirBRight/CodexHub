from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from providers_config import ModelConfig, ProviderConfig, load_providers, save_providers


class ProvidersConfigTests(unittest.TestCase):
    def test_missing_file_returns_empty_provider_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.toml"

            self.assertEqual(load_providers(path), [])

    def test_default_config_uses_provider_ids_that_match_model_slug_prefixes(self):
        providers = load_providers()

        self.assertEqual([provider.id for provider in providers], ["ollama-cloud", "volc", "minimax-cn"])

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
