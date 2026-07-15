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
    build_ollama_cloud_model_index,
    catalog_visible_ollama_cloud_models,
    discover_official_models,
    discover_provider_models,
    load_providers,
    resolve_external_model_alias,
    resolve_ollama_cloud_model,
    save_providers,
)


class ProvidersConfigTests(unittest.TestCase):
    def test_bundled_ollama_glm_uses_the_only_deferred_core_model_override(self):
        providers = load_providers(DEFAULT_PROVIDERS_PATH)
        ollama = next(provider for provider in providers if provider.id == "ollama-cloud")
        configured_models = [
            model.id for provider in providers for model in provider.models if model.tool_surface_strategy is not None
        ]

        self.assertEqual(ollama.tool_surface_strategy, "eager")
        self.assertEqual(
            next(model.tool_surface_strategy for model in ollama.models if model.id == "glm-5.2"),
            "deferred_core",
        )
        self.assertEqual(configured_models, ["glm-5.2"])

    def test_bundled_ollama_glm_selects_the_only_strict_apply_patch_native_responses_codec(self):
        providers = load_providers(DEFAULT_PROVIDERS_PATH)
        configured, index = build_ollama_cloud_model_index(providers, require_api_key=False)
        configured_models = [
            model.id
            for provider in providers
            for model in provider.models
            if model.native_responses_tool_codec is not None
        ]

        self.assertTrue(configured)
        self.assertEqual(configured_models, ["glm-5.2"])
        self.assertEqual(index["glm-5.2"]["native_responses_tool_codec"], "strict_apply_patch")
        self.assertEqual(index["ollama-cloud/glm-5.2"]["native_responses_tool_codec"], "strict_apply_patch")
        self.assertEqual(index["minimax-m3"]["native_responses_tool_codec"], "none")

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

    def test_tool_surface_strategy_resolves_provider_default_and_model_override_in_both_indexes(self):
        generic = build_external_model_index(
            [
                ProviderConfig(
                    id="volc",
                    name="Volcengine",
                    base_url="https://volc.example/v1",
                    api_key="volc-secret",
                    tool_surface_strategy="deferred_core",
                    models=[
                        ModelConfig(id="provider-default"),
                        ModelConfig(id="model-override", tool_surface_strategy="eager"),
                    ],
                ),
                ProviderConfig(
                    id="minimax-cn",
                    name="MiniMax",
                    base_url="https://minimax.example/v1",
                    api_key="minimax-secret",
                    models=[ModelConfig(id="implicit-default")],
                ),
            ],
            require_api_key=False,
        )
        configured, ollama = build_ollama_cloud_model_index(
            [
                ProviderConfig(
                    id="ollama-cloud",
                    name="Ollama Cloud",
                    base_url="https://ollama.example/v1",
                    api_key="ollama-secret",
                    tool_surface_strategy="eager",
                    models=[
                        ModelConfig(id="glm-5.2", tool_surface_strategy="deferred_core"),
                        ModelConfig(id="minimax-m3"),
                    ],
                )
            ],
            require_api_key=False,
        )

        self.assertEqual(generic["volc/provider-default"]["tool_surface_strategy"], "deferred_core")
        self.assertEqual(generic["volc/model-override"]["tool_surface_strategy"], "eager")
        self.assertEqual(generic["minimax-cn/implicit-default"]["tool_surface_strategy"], "eager")
        self.assertTrue(configured)
        self.assertEqual(ollama["glm-5.2"]["tool_surface_strategy"], "deferred_core")
        self.assertEqual(ollama["ollama-cloud/glm-5.2"]["tool_surface_strategy"], "deferred_core")
        self.assertEqual(ollama["minimax-m3"]["tool_surface_strategy"], "eager")

    def test_runtime_omission_inherits_bundled_model_strategy_for_ollama_cloud_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.example.test/v1"
api_key = "ollama-secret"

  [[providers.models]]
  id = "glm-5.2"
""".lstrip(),
                encoding="utf-8",
            )

            configured, unqualified = resolve_ollama_cloud_model(
                "glm-5.2", providers_path=path, require_api_key=False
            )
            qualified_configured, qualified = resolve_ollama_cloud_model(
                "ollama-cloud/glm-5.2", providers_path=path, require_api_key=False
            )

        self.assertTrue(configured)
        self.assertTrue(qualified_configured)
        self.assertEqual(unqualified["tool_surface_strategy"], "deferred_core")
        self.assertEqual(qualified["tool_surface_strategy"], "deferred_core")

    def test_runtime_omission_inherits_bundled_native_responses_tool_codec_for_ollama_glm_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.example.test/v1"
api_key = "ollama-secret"

  [[providers.models]]
  id = "glm-5.2"
""".lstrip(),
                encoding="utf-8",
            )

            configured, unqualified = resolve_ollama_cloud_model(
                "glm-5.2", providers_path=path, require_api_key=False
            )
            qualified_configured, qualified = resolve_ollama_cloud_model(
                "ollama-cloud/glm-5.2", providers_path=path, require_api_key=False
            )

        self.assertTrue(configured)
        self.assertTrue(qualified_configured)
        self.assertEqual(unqualified["native_responses_tool_codec"], "strict_apply_patch")
        self.assertEqual(qualified["native_responses_tool_codec"], "strict_apply_patch")

    def test_inherited_ollama_strategy_prepares_a_deferred_core_tool_surface(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.example.test/v1"
api_key = "ollama-secret"

  [[providers.models]]
  id = "glm-5.2"
""".lstrip(),
                encoding="utf-8",
            )
            _, resolved = resolve_ollama_cloud_model(
                "glm-5.2", providers_path=path, require_api_key=False
            )

        shell_command = {
            "type": "function",
            "name": "shell_command",
            "description": "Run a PowerShell command.",
            "parameters": {"type": "object", "properties": {}},
        }
        apply_patch = {
            "type": "custom",
            "name": "apply_patch",
            "description": "Apply a unified diff patch.",
        }
        namespace = {
            "type": "namespace",
            "name": "mcp__synthetic_namespace",
            "description": "Synthetic namespace used to prove the prepared tool surface.",
            "tools": [
                {
                    "type": "function",
                    "name": f"synthetic_tool_{index:03d}",
                    "parameters": {"type": "object", "properties": {}},
                }
                for index in range(200)
            ],
        }
        from codex_proxy import compatible_request_body

        prepared = json.loads(
            compatible_request_body(
                json.dumps(
                    {
                        "model": "glm-5.2",
                        "input": "Use the visible core tools only.",
                        "tools": [shell_command, apply_patch, namespace],
                    }
                ).encode("utf-8"),
                {"name": resolved["upstream_name"], **resolved},
            )
        )
        prepared_tools = prepared["tools"]
        prepared_names = {
            tool["name"]
            for tool in prepared_tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }

        self.assertEqual(resolved["tool_surface_strategy"], "deferred_core")
        self.assertEqual(prepared_tools[:2], [shell_command, apply_patch])
        self.assertFalse(any(tool.get("type") == "namespace" for tool in prepared_tools))
        self.assertFalse(
            any(name.startswith("mcp__synthetic_namespace__synthetic_tool_") for name in prepared_names)
        )
        self.assertIn("tool_search", prepared_names)

    def test_runtime_omission_inherits_bundled_provider_strategy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundled_path = root / "bundled-providers.toml"
            runtime_path = root / "runtime-providers.toml"
            bundled_path.write_text(
                """
[[providers]]
id = "generic-provider"
name = "Generic Provider"
base_url = "https://bundled.example.test/v1"
api_key = "bundled-secret"
tool_surface_strategy = "deferred_core"

  [[providers.models]]
  id = "generic-model"
""".lstrip(),
                encoding="utf-8",
            )
            runtime_path.write_text(
                """
[[providers]]
id = "generic-provider"
name = "Generic Provider"
base_url = "https://runtime.example.test/v1"
api_key = "runtime-secret"

  [[providers.models]]
  id = "generic-model"
""".lstrip(),
                encoding="utf-8",
            )

            with patch("providers_config.DEFAULT_PROVIDERS_PATH", bundled_path):
                index = build_external_model_index(load_providers(runtime_path), require_api_key=False)

        self.assertEqual(index["generic-provider/generic-model"]["tool_surface_strategy"], "deferred_core")

    def test_runtime_bundled_model_override_wins_over_explicit_provider_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundled_path = root / "bundled-providers.toml"
            runtime_path = root / "runtime-providers.toml"
            saved_path = root / "saved-providers.toml"
            bundled_path.write_text(
                """
[[providers]]
id = "generic-provider"
name = "Generic Provider"
base_url = "https://bundled.example.test/v1"
api_key = "bundled-secret"
tool_surface_strategy = "deferred_core"

  [[providers.models]]
  id = "known-model"
  tool_surface_strategy = "deferred_core"
""".lstrip(),
                encoding="utf-8",
            )
            runtime_path.write_text(
                """
[[providers]]
id = "generic-provider"
name = "Generic Provider"
base_url = "https://runtime.example.test/v1"
api_key = "runtime-secret"
tool_surface_strategy = "eager"

  [[providers.models]]
  id = "known-model"

  [[providers.models]]
  id = "unrelated-model"
""".lstrip(),
                encoding="utf-8",
            )

            with patch("providers_config.DEFAULT_PROVIDERS_PATH", bundled_path):
                loaded = load_providers(runtime_path)
                initial_index = build_external_model_index(loaded, require_api_key=False)
                save_providers(loaded, saved_path)
                saved_toml = saved_path.read_text(encoding="utf-8")
                reloaded_index = build_external_model_index(
                    load_providers(saved_path), require_api_key=False
                )

        self.assertIsNone(loaded[0].models[0].tool_surface_strategy)
        self.assertEqual(initial_index["generic-provider/known-model"]["tool_surface_strategy"], "deferred_core")
        self.assertEqual(initial_index["generic-provider/unrelated-model"]["tool_surface_strategy"], "eager")
        self.assertIn('tool_surface_strategy = "eager"', saved_toml)
        self.assertNotIn('  tool_surface_strategy = "deferred_core"', saved_toml)
        self.assertEqual(reloaded_index["generic-provider/known-model"]["tool_surface_strategy"], "deferred_core")
        self.assertEqual(reloaded_index["generic-provider/unrelated-model"]["tool_surface_strategy"], "eager")

    def test_runtime_explicit_model_strategy_overrides_bundled_model_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.example.test/v1"
api_key = "ollama-secret"

  [[providers.models]]
  id = "glm-5.2"
  tool_surface_strategy = "eager"
""".lstrip(),
                encoding="utf-8",
            )

            _, resolved = resolve_ollama_cloud_model(
                "glm-5.2", providers_path=path, require_api_key=False
            )

        self.assertEqual(resolved["tool_surface_strategy"], "eager")

    def test_runtime_omission_inherits_bundled_model_strategy_through_canonical_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundled_path = root / "bundled-providers.toml"
            runtime_path = root / "runtime-providers.toml"
            bundled_path.write_text(
                """
[[providers]]
id = "alias-provider"
name = "Alias Provider"
base_url = "https://bundled.example.test/v1"
api_key = "bundled-secret"

  [[providers.models]]
  id = "canonical-model"
  aliases = ["legacy-model:cloud"]
  tool_surface_strategy = "deferred_core"
""".lstrip(),
                encoding="utf-8",
            )
            runtime_path.write_text(
                """
[[providers]]
id = "alias-provider"
name = "Alias Provider"
base_url = "https://runtime.example.test/v1"
api_key = "runtime-secret"

  [[providers.models]]
  id = " legacy-model "
""".lstrip(),
                encoding="utf-8",
            )

            with patch("providers_config.DEFAULT_PROVIDERS_PATH", bundled_path):
                index = build_external_model_index(load_providers(runtime_path), require_api_key=False)

        self.assertEqual(index["alias-provider/legacy-model"]["tool_surface_strategy"], "deferred_core")

    def test_runtime_omission_fails_closed_without_readable_bundled_defaults(self):
        for label, bundled_text in (("missing", None), ("malformed", "[[providers]\n")):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                bundled_path = root / "bundled-providers.toml"
                runtime_path = root / "runtime-providers.toml"
                if bundled_text is not None:
                    bundled_path.write_text(bundled_text, encoding="utf-8")
                runtime_path.write_text(
                    """
[[providers]]
id = "runtime-provider"
name = "Runtime Provider"
base_url = "https://runtime.example.test/v1"
api_key = "runtime-secret"

  [[providers.models]]
  id = "runtime-model"
""".lstrip(),
                    encoding="utf-8",
                )

                with patch("providers_config.DEFAULT_PROVIDERS_PATH", bundled_path):
                    with self.assertRaisesRegex(
                        ValueError, "bundled providers configuration is required"
                    ):
                        load_providers(runtime_path)

    def test_explicit_runtime_model_strategy_does_not_require_bundled_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bundled_path = root / "missing-bundled-providers.toml"
            runtime_path = root / "runtime-providers.toml"
            runtime_path.write_text(
                """
[[providers]]
id = "runtime-provider"
name = "Runtime Provider"
base_url = "https://runtime.example.test/v1"
api_key = "runtime-secret"

  [[providers.models]]
  id = "runtime-model"
  tool_surface_strategy = "eager"
""".lstrip(),
                encoding="utf-8",
            )

            with patch("providers_config.DEFAULT_PROVIDERS_PATH", bundled_path):
                index = build_external_model_index(load_providers(runtime_path), require_api_key=False)

        self.assertEqual(index["runtime-provider/runtime-model"]["tool_surface_strategy"], "eager")

    def test_tool_surface_strategy_rejects_invalid_provider_and_model_configuration(self):
        for provider_strategy, model_strategy in (("unknown", None), ("eager", "unknown")):
            with self.subTest(provider_strategy=provider_strategy, model_strategy=model_strategy):
                with self.assertRaisesRegex(ValueError, "tool_surface_strategy"):
                    build_external_model_index(
                        [
                            ProviderConfig(
                                id="invalid",
                                name="Invalid",
                                base_url="https://invalid.example/v1",
                                api_key="test-secret",
                                tool_surface_strategy=provider_strategy,
                                models=[ModelConfig(id="invalid-model", tool_surface_strategy=model_strategy)],
                            )
                        ],
                        require_api_key=False,
                    )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "invalid"
name = "Invalid"
base_url = "https://invalid.example/v1"
api_key = "test-secret"

  [[providers.models]]
  id = "invalid-model"
  tool_surface_strategy = "unknown"
""".lstrip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "tool_surface_strategy"):
                load_providers(path)

    def test_upstream_model_load_save_and_index_preserve_live_case(self):
        providers = [
            ProviderConfig(
                id="case-provider",
                name="Case Provider",
                base_url="https://case.example/v1",
                api_key="case-secret",
                upstream_format="chat_completions",
                available_upstream_formats=("responses", "chat_completions"),
                tool_protocol="chat_tools",
                tool_surface_strategy="deferred_core",
                models=[
                    ModelConfig(
                        id="alias-model",
                        upstream_model="Live-Case-Model",
                        aliases=("legacy-case-model",),
                        input_modalities=("text", "image"),
                        supported_reasoning_levels=("low", "high", "xhigh"),
                        default_reasoning_level="high",
                        tool_surface_strategy="eager",
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
        self.assertEqual(loaded[0].available_upstream_formats, ("responses", "chat_completions"))
        self.assertEqual(loaded[0].tool_protocol, "chat_tools")
        self.assertEqual(loaded[0].tool_surface_strategy, "deferred_core")
        self.assertEqual(loaded[0].models[0].tool_surface_strategy, "eager")
        self.assertIsNone(loaded[0].models[1].upstream_model)
        self.assertIn('upstream_model = "Live-Case-Model"', raw_toml)
        self.assertIn('aliases = ["legacy-case-model"]', raw_toml)
        self.assertIn('input_modalities = ["text", "image"]', raw_toml)
        self.assertIn('supported_reasoning_levels = ["low", "high", "xhigh"]', raw_toml)
        self.assertIn('default_reasoning_level = "high"', raw_toml)
        self.assertIn('upstream_format = "chat_completions"', raw_toml)
        self.assertIn('available_upstream_formats = ["responses", "chat_completions"]', raw_toml)
        self.assertIn('tool_protocol = "chat_tools"', raw_toml)
        self.assertIn('tool_surface_strategy = "deferred_core"', raw_toml)
        self.assertIn('  tool_surface_strategy = "eager"', raw_toml)

        index = build_external_model_index(loaded)
        self.assertEqual(index["case-provider/alias-model"]["upstream_model"], "Live-Case-Model")
        self.assertEqual(index["case-provider/alias-model"]["upstream_format"], "chat_completions")
        self.assertEqual(index["case-provider/alias-model"]["tool_protocol"], "chat_tools")
        self.assertEqual(index["case-provider/alias-model"]["tool_surface_strategy"], "eager")
        self.assertEqual(index["case-provider/alias-model"]["input_modalities"], ("text", "image"))
        self.assertEqual(index["case-provider/alias-model"]["supported_reasoning_levels"], ("low", "high", "xhigh"))
        self.assertEqual(index["case-provider/alias-model"]["default_reasoning_level"], "high")
        self.assertEqual(index["case-provider/Fallback-Model"]["upstream_model"], "Fallback-Model")
        self.assertEqual(index["case-provider/Fallback-Model"]["tool_surface_strategy"], "deferred_core")

    def test_save_providers_uses_atomic_writer(self):
        providers = [
            ProviderConfig(
                id="atomic-provider",
                name="Atomic Provider",
                base_url="https://atomic.example/v1",
                api_key="secret",
                models=[ModelConfig(id="atomic-model")],
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            calls: list[tuple[Path, str, str]] = []

            def capture_atomic_write(target: Path, text: str, *, encoding: str = "utf-8") -> None:
                calls.append((target, text, encoding))

            with patch("providers_config.atomic_write_text", capture_atomic_write, create=True):
                save_providers(providers, path)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], path)
        self.assertEqual(calls[0][2], "utf-8")
        self.assertIn("[[providers]]", calls[0][1])
        self.assertIn('id = "atomic-provider"', calls[0][1])

    def test_anthropic_endpoint_selection_load_save_and_index(self):
        providers = [
            ProviderConfig(
                id="anthropic-direct",
                name="Anthropic Direct",
                base_url="https://api.anthropic.com",
                api_key="{env:ANTHROPIC_API_KEY}",
                upstream_format="anthropic_messages",
                models=[ModelConfig(id="claude-sonnet-4-20250514", sort_order=1)],
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            save_providers(providers, path)
            loaded = load_providers(path)
            raw_toml = path.read_text(encoding="utf-8")

        self.assertEqual(loaded[0].upstream_format, "anthropic_messages")
        self.assertIn('upstream_format = "anthropic_messages"', raw_toml)

        index = build_external_model_index(loaded, require_api_key=False)
        self.assertEqual(
            index["anthropic-direct/claude-sonnet-4-20250514"]["upstream_format"],
            "anthropic_messages",
        )

    def test_external_model_index_preserves_exact_case_and_explicit_aliases(self):
        providers = [
            ProviderConfig(
                id="minimax-cn",
                name="MiniMax.cn",
                base_url="https://api.minimaxi.com/v1",
                api_key="minimax-secret",
                models=[
                    ModelConfig(
                        id="MiniMax-M3",
                        aliases=("minimax-m3",),
                        display_name="MiniMax-M3",
                        context_window=1000000,
                        max_output_tokens=524288,
                    )
                ],
            )
        ]

        index = build_external_model_index(providers)

        self.assertIn("minimax-cn/MiniMax-M3", index)
        self.assertIn("minimax-cn/minimax-m3", index)
        self.assertNotIn("minimax-cn/minimax-m3".upper(), index)
        self.assertEqual(index["minimax-cn/MiniMax-M3"]["alias"], "minimax-cn/MiniMax-M3")
        self.assertEqual(index["minimax-cn/minimax-m3"]["alias"], "minimax-cn/MiniMax-M3")
        self.assertEqual(index["minimax-cn/MiniMax-M3"]["upstream_model"], "MiniMax-M3")

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

    def test_build_external_model_index_ignores_legacy_hidden_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "legacy-hidden-provider"
name = "Legacy Hidden Provider"
base_url = "https://legacy.example/v1"
api_key = "secret"
enabled = true
hidden = true

  [[providers.models]]
  id = "exported"
  enabled = true
  hidden = true
  gateway_exported = true

  [[providers.models]]
  id = "not-exported"
  enabled = true
  hidden = true
  gateway_exported = false

  [[providers.models]]
  id = "disabled"
  enabled = false
  hidden = true
  gateway_exported = true
""".strip(),
                encoding="utf-8",
            )
            providers = load_providers(path)

        index = build_external_model_index(providers)

        self.assertEqual(sorted(index), ["legacy-hidden-provider/exported"])

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

    def test_ollama_cloud_runtime_index_uses_enabled_gateway_exported_models(self):
        providers = [
            ProviderConfig(
                id="ollama-cloud",
                name="Ollama Cloud",
                base_url="https://ollama.example.test/v1",
                api_key="ollama-secret",
                upstream_format="responses",
                models=[
                    ModelConfig(
                        id="runtime-model",
                        aliases=("runtime-alias",),
                        context_window=123000,
                        max_output_tokens=456,
                    ),
                    ModelConfig(id="disabled-model", enabled=False),
                    ModelConfig(id="hidden-model", gateway_exported=False),
                ],
            )
        ]

        configured, index = build_ollama_cloud_model_index(providers)
        visible_configured, visible_models = catalog_visible_ollama_cloud_models(providers)

        self.assertTrue(configured)
        self.assertTrue(visible_configured)
        self.assertEqual(sorted(index), ["ollama-cloud/runtime-alias", "ollama-cloud/runtime-model", "runtime-alias", "runtime-model"])
        self.assertEqual([model["alias"] for model in visible_models], ["ollama-cloud/runtime-model"])
        self.assertEqual(index["runtime-model"]["upstream_name"], "ollama_cloud")
        self.assertEqual(index["runtime-alias"]["upstream_model"], "runtime-model")
        self.assertEqual(index["runtime-model"]["context_window"], 123000)
        self.assertEqual(index["runtime-model"]["max_output_tokens"], 456)

    def test_resolve_ollama_cloud_model_reports_configured_disabled_and_unknown_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "providers.toml"
            path.write_text(
                """
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.example.test/v1"
api_key = "ollama-secret"
enabled = true

  [[providers.models]]
  id = "runtime-model"
  enabled = true

  [[providers.models]]
  id = "disabled-model"
  enabled = false
""".lstrip(),
                encoding="utf-8",
            )

            configured, resolved = resolve_ollama_cloud_model("ollama-cloud/runtime-model", providers_path=path)
            disabled_configured, disabled = resolve_ollama_cloud_model("disabled-model", providers_path=path)
            unknown_configured, unknown = resolve_ollama_cloud_model("unknown-model", providers_path=path)

        self.assertTrue(configured)
        self.assertEqual(resolved["upstream_model"], "runtime-model")
        self.assertTrue(disabled_configured)
        self.assertIsNone(disabled)
        self.assertTrue(unknown_configured)
        self.assertIsNone(unknown)

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

        self.assertEqual([provider.id for provider in providers], ["ollama-cloud", "volc", "minimax-cn", "xunfei"])

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

        self.assertEqual(
            sorted(index),
            [
                "minimax-cn/MiniMax-M3",
                "minimax-cn/minimax-m3",
                "volc/glm-5.2",
                "volc/kimi-k2.6",
            ],
        )
        self.assertNotIn("volc/minimax-m3", index)
        self.assertEqual(index["minimax-cn/MiniMax-M3"]["upstream_model"], "MiniMax-M3")
        self.assertEqual(index["minimax-cn/minimax-m3"]["alias"], "minimax-cn/MiniMax-M3")
        self.assertEqual(index["volc/kimi-k2.6"]["upstream_model"], "kimi-k2.6")
        self.assertIsNone(volc_minimax)

    def test_default_policy_preserves_provider_qualified_catalog_models(self):
        from catalog import load_policy

        policy = load_policy(Path("config/catalog_policy.toml"))

        self.assertTrue(
            {
                "ollama-cloud/glm-5.2",
                "ollama-cloud/minimax-m3",
                "ollama-cloud/kimi-k2.6",
                "volc/ark-code-latest",
                "volc/doubao-seed-2.0-code",
                "volc/doubao-seed-2.0-pro",
                "volc/doubao-seed-2.0-lite",
                "volc/glm-5.2",
                "volc/deepseek-v4-pro",
                "volc/deepseek-v4-flash",
                "volc/kimi-k2.6",
                "minimax-cn/MiniMax-M3",
            }.issubset(policy.allowed_provider_models)
        )
        self.assertNotIn("minimax-cn/minimax-m3", policy.allowed_provider_models)
        self.assertIn("glm-5.2", policy.allowed_ollama_cloud_models)

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
        self.assertNotIn("hidden", raw_toml)
        self.assertIn("enabled = true", raw_toml)
        self.assertIn("gateway_exported = true", raw_toml)

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

