from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from providers_config import (
    DEFAULT_PROVIDERS_PATH,
    ModelConfig,
    ProviderConfig,
    build_external_model_index,
    discover_official_models,
    discover_provider_models,
    load_providers,
    resolve_external_model_alias,
    save_providers,
)


class ProvidersConfigTests(unittest.TestCase):
    def test_discover_official_models_fetches_gpt_models_sorted_with_limits(self):
        payload = {
            "data": [
                {"id": " gpt-4.1-mini ", "context_window": 128000, "max_output_tokens": 32768},
                {"id": "gpt-4.1", "context_length": "1047576", "output_tokens": "32768"},
                {"model": "gpt-4o", "limit": {"context": 128000, "output": 16384}},
                {"id": "gpt-4.1", "context_window": 1, "max_output_tokens": 1},
                {"id": "chatgpt-4o-latest"},
                {"id": "o3"},
                {"id": "  "},
                {"id": 123},
            ]
        }

        mock_response = unittest.mock.Mock()
        mock_response.__enter__ = unittest.mock.Mock(return_value=mock_response)
        mock_response.__exit__ = unittest.mock.Mock(return_value=None)
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")

        with patch("providers_config.urlopen", return_value=mock_response) as mock_urlopen:
            models = discover_official_models(" test-secret ", timeout_seconds=9)

        self.assertEqual(
            models,
            [
                {"id": "gpt-4.1", "context_window": 1047576, "max_output_tokens": 32768},
                {"id": "gpt-4.1-mini", "context_window": 128000, "max_output_tokens": 32768},
                {"id": "gpt-4o", "context_window": 128000, "max_output_tokens": 16384},
            ],
        )
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(mock_urlopen.call_args.kwargs, {"timeout": 9})

    def test_discover_official_models_accepts_models_key_and_bare_list_payloads(self):
        cases = [
            (
                {
                    "models": [
                        {"id": "gpt-from-models-b", "max_output_tokens": 2048},
                        {"id": "gpt-from-models-a", "context_window": 64000},
                        {"id": "gpt-from-models-b", "context_window": 1, "max_output_tokens": 1},
                        {"id": "chatgpt-from-models"},
                        {"id": "  "},
                    ]
                },
                [
                    {"id": "gpt-from-models-a", "context_window": 64000, "max_output_tokens": None},
                    {"id": "gpt-from-models-b", "context_window": None, "max_output_tokens": 2048},
                ],
            ),
            (
                [
                    {"slug": "gpt-from-list-c", "context_length": "32000"},
                    "gpt-from-list-a",
                    {"model": " gpt-from-list-b ", "output_tokens": "1024"},
                    {"name": "gpt-from-list-c", "context_window": 1, "max_output_tokens": 1},
                    {"id": "o3"},
                    {"id": "not-gpt-from-list"},
                    {"id": "  "},
                ],
                [
                    {"id": "gpt-from-list-a", "context_window": None, "max_output_tokens": None},
                    {"id": "gpt-from-list-b", "context_window": None, "max_output_tokens": 1024},
                    {"id": "gpt-from-list-c", "context_window": 32000, "max_output_tokens": None},
                ],
            ),
        ]

        for payload, expected in cases:
            with self.subTest(payload_type=type(payload).__name__):
                mock_response = unittest.mock.Mock()
                mock_response.__enter__ = unittest.mock.Mock(return_value=mock_response)
                mock_response.__exit__ = unittest.mock.Mock(return_value=None)
                mock_response.read.return_value = json.dumps(payload).encode("utf-8")

                with patch("providers_config.urlopen", return_value=mock_response):
                    models = discover_official_models("test-secret")

                self.assertEqual(models, expected)
                for model in models:
                    self.assertEqual(set(model), {"id", "context_window", "max_output_tokens"})

    def test_discover_provider_models_fetches_models_and_normalizes_response(self):
        payload = {
            "data": [
                {"id": "alpha", "context_window": 128000, "max_output_tokens": 8192},
                {"model": "beta", "max_context_window": 64000, "output_tokens": 4096},
                {"name": "nested", "limit": {"context": 32000, "output": 2048}},
                "string-model",
                {"slug": "alpha", "context_length": 1, "max_output_tokens": 1},
                {"id": "  "},
            ]
        }

        mock_response = unittest.mock.Mock()
        mock_response.__enter__ = unittest.mock.Mock(return_value=mock_response)
        mock_response.__exit__ = unittest.mock.Mock(return_value=None)
        mock_response.read.return_value = json.dumps(payload).encode("utf-8")

        with patch("providers_config.urlopen", return_value=mock_response) as mock_urlopen:
            models = discover_provider_models("https://example.test/v1/", " test-secret ", timeout_seconds=7)

        self.assertEqual(
            models,
            [
                {"id": "alpha", "context_window": 128000, "max_output_tokens": 8192},
                {"id": "beta", "context_window": 64000, "max_output_tokens": 4096},
                {"id": "nested", "context_window": 32000, "max_output_tokens": 2048},
                {"id": "string-model", "context_window": None, "max_output_tokens": None},
            ],
        )
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.test/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(mock_urlopen.call_args.kwargs, {"timeout": 7})

    def test_discover_provider_models_accepts_models_key_and_bare_list_payloads(self):
        cases = [
            (
                {"models": [{"slug": "from-models", "context_length": 1024}]},
                [{"id": "from-models", "context_window": 1024, "max_output_tokens": None}],
            ),
            (
                [{"id": "from-list", "max_output_tokens": 256}],
                [{"id": "from-list", "context_window": None, "max_output_tokens": 256}],
            ),
        ]

        for payload, expected in cases:
            with self.subTest(payload_type=type(payload).__name__):
                mock_response = unittest.mock.Mock()
                mock_response.__enter__ = unittest.mock.Mock(return_value=mock_response)
                mock_response.__exit__ = unittest.mock.Mock(return_value=None)
                mock_response.read.return_value = json.dumps(payload).encode("utf-8")

                with patch("providers_config.urlopen", return_value=mock_response) as mock_urlopen:
                    models = discover_provider_models("https://example.test/v1", "  ")

                self.assertEqual(models, expected)
                request = mock_urlopen.call_args.args[0]
                self.assertEqual(request.full_url, "https://example.test/v1/models")
                self.assertIsNone(request.get_header("Authorization"))

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
  upstream_model = "MiniMax-M3"
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
        self.assertEqual(minimax["upstream_model"], "MiniMax-M3")
        self.assertEqual(minimax["context_window"], 1000000)
        self.assertEqual(minimax["max_output_tokens"], 524288)
        self.assertEqual(minimax["priority_base"], 300)

    def test_upstream_model_load_save_and_index_preserve_live_case(self):
        providers = [
            ProviderConfig(
                id="case-provider",
                name="Case Provider",
                base_url="https://case.example/v1",
                api_key="case-secret",
                upstream_format="chat_completions",
                models=[
                    ModelConfig(
                        id="alias-model",
                        upstream_model="Live-Case-Model",
                        input_modalities=("text", "image"),
                        supported_reasoning_levels=("low", "high", "xhigh"),
                        default_reasoning_level="high",
                        sort_order=1,
                    ),
                    ModelConfig(
                        id=" Fallback-Model ",
                        sort_order=2,
                    ),
                ],
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            save_providers(providers, path)
            loaded = load_providers(path)
            raw_toml = path.read_text(encoding="utf-8")

        self.assertEqual(loaded[0].models[0].upstream_model, "Live-Case-Model")
        self.assertEqual(loaded[0].models[0].input_modalities, ("text", "image"))
        self.assertEqual(loaded[0].models[0].supported_reasoning_levels, ("low", "high", "xhigh"))
        self.assertEqual(loaded[0].models[0].default_reasoning_level, "high")
        self.assertEqual(loaded[0].upstream_format, "chat_completions")
        self.assertIsNone(loaded[0].models[1].upstream_model)
        self.assertIn('upstream_model = "Live-Case-Model"', raw_toml)
        self.assertIn('input_modalities = ["text", "image"]', raw_toml)
        self.assertIn('supported_reasoning_levels = ["low", "high", "xhigh"]', raw_toml)
        self.assertIn('default_reasoning_level = "high"', raw_toml)
        self.assertIn('upstream_format = "chat_completions"', raw_toml)

        index = build_external_model_index(loaded)
        self.assertEqual(index["case-provider/alias-model"]["upstream_model"], "Live-Case-Model")
        self.assertEqual(index["case-provider/alias-model"]["upstream_format"], "chat_completions")
        self.assertEqual(index["case-provider/alias-model"]["input_modalities"], ("text", "image"))
        self.assertEqual(index["case-provider/alias-model"]["supported_reasoning_levels"], ("low", "high", "xhigh"))
        self.assertEqual(index["case-provider/alias-model"]["default_reasoning_level"], "high")
        self.assertEqual(index["case-provider/fallback-model"]["upstream_model"], "Fallback-Model")

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

    def test_build_external_model_index_skips_hidden_and_not_exported_models(self):
        providers = [
            ProviderConfig(
                id="visible-provider",
                name="Visible Provider",
                base_url="https://visible.example/v1",
                api_key="secret",
                models=[
                    ModelConfig(id="exported", gateway_exported=True, hidden=False),
                    ModelConfig(id="not-exported", gateway_exported=False, hidden=False),
                    ModelConfig(id="hidden", gateway_exported=True, hidden=True),
                ],
            ),
            ProviderConfig(
                id="hidden-provider",
                name="Hidden Provider",
                base_url="https://hidden.example/v1",
                api_key="secret",
                hidden=True,
                models=[ModelConfig(id="exported")],
            ),
        ]

        index = build_external_model_index(providers)

        self.assertEqual(sorted(index), ["visible-provider/exported"])

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

    def test_build_external_model_index_can_include_models_without_api_keys_for_catalogs(self):
        providers = [
            ProviderConfig(
                id="missing-key",
                name="Missing Key",
                base_url="https://missing.example/v1",
                api_key="{env:PROVIDERS_CONFIG_MISSING_EXTERNAL_KEY}",
                models=[ModelConfig(id="model")],
            ),
        ]

        with patch.dict("os.environ", {}, clear=True):
            index = build_external_model_index(providers, require_api_key=False)

        self.assertEqual(sorted(index), ["missing-key/model"])
        self.assertIsNone(index["missing-key/model"]["api_key"])

    def test_build_external_model_index_skips_whitespace_env_keys_and_invalid_routing_primitives(self):
        providers = [
            ProviderConfig(
                id="whitespace-env",
                name="Whitespace Env",
                base_url="https://whitespace-env.example/v1",
                api_key="{env:PROVIDERS_CONFIG_WHITESPACE_EXTERNAL_KEY}",
                models=[ModelConfig(id="model")],
            ),
            ProviderConfig(
                id="   ",
                name="Blank Provider Id",
                base_url="https://blank-provider.example/v1",
                api_key="blank-provider-secret",
                models=[ModelConfig(id="model")],
            ),
            ProviderConfig(
                id="blank-base",
                name="Blank Base URL",
                base_url="  \t  ",
                api_key="blank-base-secret",
                models=[ModelConfig(id="model")],
            ),
            ProviderConfig(
                id="blank-model",
                name="Blank Model",
                base_url="https://blank-model.example/v1",
                api_key="blank-model-secret",
                models=[ModelConfig(id="   ")],
            ),
            ProviderConfig(
                id="valid",
                name="Valid Provider",
                base_url="  https://valid.example/v1  ",
                api_key=" valid-secret ",
                models=[ModelConfig(id="model")],
            ),
        ]

        with patch.dict(
            "os.environ",
            {"PROVIDERS_CONFIG_WHITESPACE_EXTERNAL_KEY": "  \t  "},
            clear=True,
        ):
            index = build_external_model_index(providers)

        self.assertEqual(sorted(index), ["valid/model"])
        self.assertEqual(index["valid/model"]["base_url"], "https://valid.example/v1")
        self.assertEqual(index["valid/model"]["api_key"], "valid-secret")

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
        providers = load_providers(DEFAULT_PROVIDERS_PATH)

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
            index = build_external_model_index(load_providers(DEFAULT_PROVIDERS_PATH))
            volc_minimax = resolve_external_model_alias("volc/minimax-m3", providers_path=DEFAULT_PROVIDERS_PATH)

        self.assertEqual(sorted(index), ["minimax-cn/minimax-m3", "volc/glm-5.2", "volc/kimi-k2.6"])
        self.assertNotIn("volc/minimax-m3", index)
        self.assertEqual(index["minimax-cn/minimax-m3"]["upstream_model"], "MiniMax-M3")
        self.assertEqual(index["volc/kimi-k2.6"]["upstream_model"], "kimi-k2.6")
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

    def test_resolved_api_key_strips_literals_padded_placeholders_and_env_values(self):
        whitespace_provider = ProviderConfig(
            id="whitespace",
            name="Whitespace Provider",
            base_url="https://whitespace.example/v1",
            api_key="  \t  ",
        )
        padded_env_provider = ProviderConfig(
            id="padded-env",
            name="Padded Env Provider",
            base_url="https://padded.example/v1",
            api_key="  {env:PROVIDERS_CONFIG_PADDED_TEST_KEY}  ",
        )
        whitespace_env_provider = ProviderConfig(
            id="whitespace-env",
            name="Whitespace Env Provider",
            base_url="https://whitespace-env.example/v1",
            api_key="{env:PROVIDERS_CONFIG_WHITESPACE_TEST_KEY}",
        )
        padded_literal_provider = ProviderConfig(
            id="padded-literal",
            name="Padded Literal Provider",
            base_url="https://literal.example/v1",
            api_key="  literal-secret  ",
        )

        with patch.dict(
            "os.environ",
            {
                "PROVIDERS_CONFIG_PADDED_TEST_KEY": "  env-secret  ",
                "PROVIDERS_CONFIG_WHITESPACE_TEST_KEY": "  \t  ",
            },
            clear=True,
        ):
            self.assertEqual(padded_env_provider.resolved_api_key(), "env-secret")
            self.assertIsNone(whitespace_env_provider.resolved_api_key())

        self.assertIsNone(whitespace_provider.resolved_api_key())
        self.assertEqual(padded_literal_provider.resolved_api_key(), "literal-secret")

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

    def test_runtime_providers_path_preferred_over_bundled(self):
        """Regression: runtime CODEX_HOME/providers.toml must be read by load_providers
        and resolve_external_model_alias, matching the Rust backend write path."""
        import importlib
        import providers_config

        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            runtime_providers = codex_home / "proxy" / "config" / "providers.toml"
            runtime_providers.parent.mkdir(parents=True)
            runtime_providers.write_text(
                """
[[providers]]
id = "test-runtime"
name = "Test Runtime Provider"
base_url = "https://runtime.example/v1"
api_key = "runtime-key"

  [[providers.models]]
  id = "runtime-model"
  context_window = 64000
""",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                importlib.reload(providers_config)
                resolved_path = providers_config.runtime_providers_path()
                self.assertEqual(resolved_path, runtime_providers)

                loaded = providers_config.load_providers()
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].id, "test-runtime")

                alias = providers_config.resolve_external_model_alias("test-runtime/runtime-model")
                self.assertIsNotNone(alias)
                self.assertEqual(alias["upstream_model"], "runtime-model")
            importlib.reload(providers_config)


if __name__ == "__main__":
    unittest.main()

