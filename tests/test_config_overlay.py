from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from config_overlay import (
    MARKER_BEGIN,
    apply_overlay,
    restore_overlay,
    catalog_config_value,
    set_feature_flags,
    strip_section,
    strip_top_level_keys,
)


class ConfigOverlayTests(unittest.TestCase):
    def test_strip_top_level_keys_does_not_touch_provider_sections(self):
        text = "\n".join(
            [
                'model = "gpt-5.5"',
                'model_provider = "openai"',
                "",
                "[model_providers.openai]",
                'model = "nested-should-stay"',
                'base_url = "https://example.test"',
                "",
            ]
        )

        cleaned = strip_top_level_keys(text)

        self.assertNotIn('model_provider = "openai"', cleaned)
        self.assertIn('model = "nested-should-stay"', cleaned)

    def test_strip_codex_proxy_section_only(self):
        text = "\n".join(
            [
                "[model_providers.codex_proxy]",
                'name = "Old Proxy"',
                "[model_providers.openai]",
                'name = "OpenAI"',
                "",
            ]
        )

        cleaned = strip_section(text, "model_providers.codex_proxy")

        self.assertNotIn("Old Proxy", cleaned)
        self.assertIn("[model_providers.openai]", cleaned)

    def test_catalog_value_is_relative_to_config_dir_when_possible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            catalog_path = tmp / "model-catalogs" / "catalog.json"

            self.assertEqual(
                catalog_config_value(config_path, catalog_path),
                "model-catalogs/catalog.json",
            )

    def test_set_feature_flags_updates_existing_features_section(self):
        text = "\n".join(
            [
                "[features]",
                "hooks = true",
                "responses_websockets = true",
                "",
                "[other]",
                "enabled = true",
            ]
        )

        updated = set_feature_flags(text, {"responses_websockets": "false", "responses_websockets_v2": "false"})

        self.assertIn("[features]", updated)
        self.assertIn("hooks = true", updated)
        self.assertIn("responses_websockets = false", updated)
        self.assertIn("responses_websockets_v2 = false", updated)
        self.assertNotIn("responses_websockets = true", updated)
        self.assertIn("[other]", updated)

    def test_apply_and_restore_overlay(self):
        original = "\n".join(
            [
                'model = "gpt-5.5"',
                'model_provider = "openai"',
                'model_reasoning_effort = "xhigh"',
                "",
                "[model_providers.codex_proxy]",
                'name = "Stale Proxy"',
                "",
                "[model_providers.openai]",
                'name = "OpenAI"',
                "",
                "[features]",
                "hooks = true",
                "responses_websockets = true",
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "model-catalogs" / "catalog.json"
            config_path.write_text(original, encoding="utf-8")

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            updated = config_path.read_text(encoding="utf-8")

            self.assertIn(MARKER_BEGIN, updated)
            self.assertIn('model = "openai/gpt-5.5"', updated)
            self.assertIn('model_provider = "custom"', updated)
            self.assertIn("model_catalog_json = 'model-catalogs/catalog.json'", updated)
            self.assertIn("[model_providers.custom]", updated)
            self.assertIn("base_url = 'http://127.0.0.1:9099/v1'", updated)
            self.assertIn('wire_api = "responses"', updated)
            self.assertIn("requires_openai_auth = true", updated)
            self.assertIn("supports_websockets = false", updated)
            self.assertNotIn("responses_websockets = true", updated)
            self.assertIn("responses_websockets = false", updated)
            self.assertIn("responses_websockets_v2 = false", updated)
            self.assertNotIn("openai_base_url", updated)
            self.assertNotIn("[model_providers.codex_proxy]", updated)
            self.assertEqual(updated.count("[model_providers.openai]"), 0)
            self.assertLess(updated.index('model_reasoning_effort = "xhigh"'), updated.index("[model_providers.custom]"))
            self.assertLess(updated.index("[model_providers.custom]"), updated.index("[features]"))

            restore_overlay(config_path, backup_path)

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertFalse(backup_path.exists())


if __name__ == "__main__":
    unittest.main()
