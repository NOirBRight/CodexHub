from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from provider_registry import configured_external_models


class ProviderRegistryTests(unittest.TestCase):
    def write_config(self, payload):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = Path(tmpdir.name) / "opencode.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_volcengine_models_from_opencode_without_minimax_cn_key(self):
        path = self.write_config(
            {
                "provider": {
                    "volcengine-plan": {
                        "options": {
                            "baseURL": "https://ark.example.test/api/coding/v3",
                            "apiKey": "volc-secret",
                        },
                        "models": {
                            "glm-5.2": {
                                "limit": {"context": 1024000, "output": 4096},
                                "modalities": {"input": ["text"], "output": ["text"]},
                            },
                            "minimax-m2.7": {
                                "limit": {"context": 200000, "output": 4096},
                            },
                        },
                    }
                }
            }
        )

        models = configured_external_models(path)

        self.assertEqual([model.alias for model in models], ["volc/glm-5.2"])
        self.assertEqual(models[0].base_url, "https://ark.example.test/api/coding/v3")
        self.assertEqual(models[0].api_key, "volc-secret")
        self.assertEqual(models[0].upstream_model, "glm-5.2")
        self.assertEqual(models[0].context_window, 1024000)
        self.assertEqual(models[0].max_output_tokens, 8192)
        self.assertEqual(models[0].max_output_source, "live_probe_2026-06-28")
        self.assertEqual(models[0].input_modalities, ("text",))

    def test_resolves_env_api_key_placeholders(self):
        path = self.write_config(
            {
                "provider": {
                    "minimax-cn": {
                        "options": {
                            "baseURL": "https://api.minimaxi.com/v1",
                            "apiKey": "{env:MINIMAX_CN_API_KEY}",
                        },
                        "models": {
                            "MiniMax-M3": {
                                "name": "MiniMax-M3",
                                "limit": {"context": 1000000, "output": 524288},
                                "modalities": {"input": ["text", "image"], "output": ["text"]},
                            }
                        },
                    }
                }
            }
        )

        with patch.dict("os.environ", {"MINIMAX_CN_API_KEY": "minimax-secret"}, clear=False):
            models = configured_external_models(path)

        self.assertEqual([model.alias for model in models], ["minimax-cn/minimax-m3"])
        self.assertEqual(models[0].api_key, "minimax-secret")
        self.assertEqual(models[0].upstream_model, "MiniMax-M3")
        self.assertEqual(models[0].context_window, 1000000)
        self.assertEqual(models[0].max_output_tokens, 524288)
        self.assertEqual(models[0].input_modalities, ("text", "image"))


if __name__ == "__main__":
    unittest.main()
