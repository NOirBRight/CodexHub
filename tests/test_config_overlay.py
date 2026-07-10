from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from config_overlay import (
    MARKER_BEGIN,
    apply_overlay,
    inject_unified_history_config,
    inspect_unified_history_config,
    main as config_overlay_main,
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

    def test_catalog_value_is_absolute_even_when_catalog_is_below_config_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            catalog_path = tmp / "model-catalogs" / "catalog.json"

            self.assertEqual(
                catalog_config_value(config_path, catalog_path),
                str(catalog_path.resolve()),
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
            self.assertIn(f"model_catalog_json = '{catalog_path.resolve()}'", updated)
            self.assertIn("[model_providers.custom]", updated)
            self.assertIn("base_url = 'http://127.0.0.1:9099/v1'", updated)
            self.assertIn('wire_api = "responses"', updated)
            self.assertIn("requires_openai_auth = false", updated)
            self.assertIn('experimental_bearer_token = "codexhub-proxy"', updated)
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

    def test_proxy_overlay_stays_non_websocket_for_phase1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "model-catalogs" / "catalog.json"

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            updated = config_path.read_text(encoding="utf-8")

            self.assertIn("supports_websockets = false", updated)
            self.assertIn("responses_websockets = false", updated)
            self.assertIn("responses_websockets_v2 = false", updated)
            self.assertNotIn("supports_websockets = true", updated)

    def test_apply_overlay_writes_owner_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "backup.toml"
            catalog = tmp / "catalog.json"

            apply_overlay(
                config,
                backup,
                catalog,
                "http://127.0.0.1:9109",
                owner="beta",
            )

            text = config.read_text(encoding="utf-8")
            self.assertIn("# owner = beta", text)
            self.assertIn("http://127.0.0.1:9109/v1", text)

    def test_apply_overlay_rejects_unknown_custom_provider_without_mutation(self):
        original = "\n".join(
            [
                'model_provider = "custom"',
                "",
                "[model_providers.custom]",
                'name = "Third Party"',
                'base_url = "https://example.test/v1"',
                'wire_api = "responses"',
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "backup.toml"
            catalog = tmp / "catalog.json"
            config.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unknown custom provider"):
                apply_overlay(config, backup, catalog, "http://127.0.0.1:9099")

            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertFalse(backup.exists())

    def test_restore_overlay_removes_owner_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "backup.toml"
            catalog = tmp / "catalog.json"

            apply_overlay(
                config,
                backup,
                catalog,
                "http://127.0.0.1:9099",
                owner="release",
            )

            restore_overlay(config, backup, unified_history=False)

            text = config.read_text(encoding="utf-8")
            self.assertNotIn("# owner = release", text)
            self.assertNotIn("# BEGIN CODEX PROXY SESSION CONFIG", text)

    def test_restore_overlay_without_backup_strips_managed_overlay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "backup.toml"
            catalog = tmp / "catalog.json"

            apply_overlay(
                config,
                backup,
                catalog,
                "http://127.0.0.1:9099",
                owner="release",
            )
            backup.unlink()

            restore_overlay(config, backup, unified_history=False)

            text = config.read_text(encoding="utf-8")
            self.assertNotIn("# owner = release", text)
            self.assertNotIn("# BEGIN CODEX PROXY SESSION CONFIG", text)

    def test_restore_overlay_can_inject_unified_official_history_bucket(self):
        original = "\n".join(
            [
                'model = "gpt-5.5"',
                'model_reasoning_effort = "high"',
                "",
                "[features]",
                "hooks = true",
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            backup_path.write_text(original, encoding="utf-8")

            status = restore_overlay(config_path, backup_path, unified_history=True)
            updated = config_path.read_text(encoding="utf-8")

            self.assertEqual(status, "injected")
            self.assertFalse(backup_path.exists())
            self.assertIn('model_provider = "custom"', updated)
            self.assertIn("[model_providers.custom]", updated)
            self.assertIn('name = "OpenAI"', updated)
            self.assertIn("requires_openai_auth = true", updated)
            self.assertIn("supports_websockets = true", updated)
            self.assertIn('wire_api = "responses"', updated)
            self.assertIn('model_reasoning_effort = "high"', updated)
            self.assertIn("[features]", updated)

    def test_restore_overlay_keeps_backup_when_config_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            config_path.write_text("overlay", encoding="utf-8")
            backup_path.write_text("original", encoding="utf-8")

            def fail_atomic_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
                if path == config_path:
                    raise OSError("simulated config write failure")
                path.write_text(text, encoding=encoding)

            with patch("config_overlay.atomic_write_text", fail_atomic_write, create=True):
                with self.assertRaisesRegex(OSError, "simulated config write failure"):
                    restore_overlay(config_path, backup_path)

            self.assertEqual(config_path.read_text(encoding="utf-8"), "overlay")
            self.assertEqual(backup_path.read_text(encoding="utf-8"), "original")

    def test_unified_history_injection_replaces_explicit_openai_provider(self):
        original = "\n".join(
            [
                'model_provider = "openai"',
                "",
                "[model_providers.openai]",
                'name = "OpenAI"',
                "",
            ]
        )

        updated, status = inject_unified_history_config(original)

        self.assertEqual(status, "injected")
        self.assertIn('model_provider = "custom"', updated)
        self.assertIn("[model_providers.custom]", updated)
        self.assertIn("[model_providers.openai]", updated)
        self.assertNotIn('model_provider = "openai"', updated)

    def test_unified_history_injection_skips_non_openai_explicit_model_provider(self):
        original = "\n".join(
            [
                'model_provider = "anthropic"',
                "",
                "[model_providers.anthropic]",
                'name = "Anthropic"',
                "",
            ]
        )

        updated, status = inject_unified_history_config(original)

        self.assertEqual(status, "explicit_model_provider")
        self.assertEqual(updated, original)

    def test_unified_history_injection_skips_conflicting_custom_provider(self):
        original = "\n".join(
            [
                "[model_providers.custom]",
                'name = "Third Party"',
                'base_url = "https://example.test/v1"',
                "",
            ]
        )

        updated, status = inject_unified_history_config(original)

        self.assertEqual(status, "conflicting_custom_provider")
        self.assertEqual(updated, original)

    def test_unified_history_injection_replaces_managed_gateway_residue(self):
        original = "\n".join(
            [
                'model = "gpt-5.6-sol"',
                "model_catalog_json = 'model-catalogs/codexhub-model-catalog.json'",
                "",
                "[model_providers.custom]",
                'name = "Codex Proxy"',
                "base_url = 'http://127.0.0.1:9099/v1'",
                'wire_api = "responses"',
                "requires_openai_auth = true",
                "supports_websockets = false",
                "",
                "[features]",
                "hooks = true",
                "",
            ]
        )

        updated, status = inject_unified_history_config(original)

        self.assertEqual(status, "replaced_managed_gateway")
        self.assertIn('model_provider = "custom"', updated)
        self.assertIn('model = "gpt-5.6-sol"', updated)
        self.assertIn('[model_providers.custom]', updated)
        self.assertIn('name = "OpenAI"', updated)
        self.assertNotIn("base_url", updated)
        self.assertNotIn("model_catalog_json", updated)
        self.assertIn("[features]", updated)

    def test_unified_history_inspection_distinguishes_active_gateway_from_drift(self):
        managed_provider = "\n".join(
            [
                "[model_providers.custom]",
                'name = "Codex Proxy"',
                "base_url = 'http://127.0.0.1:9099/v1'",
                'wire_api = "responses"',
                "requires_openai_auth = true",
                "supports_websockets = false",
                "",
            ]
        )

        active = 'model_provider = "custom"\n\n' + managed_provider
        drifted = managed_provider
        keyed_active = active.replace(
            "requires_openai_auth = true",
            'requires_openai_auth = false\nexperimental_bearer_token = "codexhub-proxy"',
        )

        self.assertEqual(inspect_unified_history_config(active), "gateway_active")
        self.assertEqual(inspect_unified_history_config(keyed_active), "gateway_active")
        self.assertEqual(inspect_unified_history_config(drifted), "needs_repair")

    def test_unified_history_inspection_reports_clean_and_conflicting_states(self):
        unified = "\n".join(
            [
                'model_provider = "custom"',
                "",
                "[model_providers.custom]",
                'name = "OpenAI"',
                "requires_openai_auth = true",
                "supports_websockets = true",
                'wire_api = "responses"',
                "",
            ]
        )
        conflicting = "\n".join(
            [
                "[model_providers.custom]",
                'name = "Third Party"',
                "base_url = 'https://example.test/v1'",
                "",
            ]
        )

        self.assertEqual(inspect_unified_history_config(unified), "clean")
        self.assertEqual(inspect_unified_history_config(conflicting), "conflict")
        self.assertEqual(inspect_unified_history_config(""), "needs_repair")
        self.assertEqual(inspect_unified_history_config(unified, unified_history=False), "needs_repair")
        self.assertEqual(inspect_unified_history_config("", unified_history=False), "clean")
        self.assertEqual(inspect_unified_history_config(conflicting, unified_history=False), "conflict")

    def test_inspect_unified_cli_emits_machine_readable_json_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.toml"
            original = 'model_provider = "openai"\n'
            config_path.write_text(original, encoding="utf-8")
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = config_overlay_main(["inspect-unified", "--config", str(config_path)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "needs_repair")
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_unified_history_injection_cleans_stale_catalog_from_official_custom_provider(self):
        original = "\n".join(
            [
                'model_provider = "custom"',
                "model_catalog_json = 'model-catalogs/codexhub-model-catalog.json'",
                "",
                "[model_providers.custom]",
                'name = "OpenAI"',
                "requires_openai_auth = true",
                "supports_websockets = true",
                'wire_api = "responses"',
                "",
            ]
        )

        updated, status = inject_unified_history_config(original)

        self.assertEqual(status, "repaired_unified")
        self.assertIn('model_provider = "custom"', updated)
        self.assertNotIn("model_catalog_json", updated)

    def test_restore_overlay_strips_exact_unified_history_bucket_when_disabled(self):
        unified = "\n".join(
            [
                'model_provider = "custom"',
                "",
                "[model_providers.custom]",
                'name = "OpenAI"',
                "requires_openai_auth = true",
                "supports_websockets = true",
                'wire_api = "responses"',
                "",
                "[features]",
                "hooks = true",
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            config_path.write_text(unified, encoding="utf-8")

            status = restore_overlay(config_path, backup_path, unified_history=False)
            updated = config_path.read_text(encoding="utf-8")

            self.assertEqual(status, "disabled")
            self.assertNotIn('model_provider = "custom"', updated)
            self.assertNotIn("[model_providers.custom]", updated)
            self.assertIn("[features]", updated)
            self.assertIn("hooks = true", updated)

    def test_restore_overlay_disabled_strips_managed_gateway_residue(self):
        managed = "\n".join(
            [
                'model_provider = "custom"',
                "model_catalog_json = 'model-catalogs/codexhub-model-catalog.json'",
                "",
                "[model_providers.custom]",
                'name = "Codex Proxy"',
                "base_url = 'http://127.0.0.1:9099/v1'",
                'wire_api = "responses"',
                "requires_openai_auth = true",
                "supports_websockets = false",
                "",
                "[features]",
                "hooks = true",
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            config_path.write_text(managed, encoding="utf-8")

            status = restore_overlay(config_path, backup_path, unified_history=False)
            updated = config_path.read_text(encoding="utf-8")

            self.assertEqual(status, "disabled")
            self.assertNotIn('model_provider = "custom"', updated)
            self.assertNotIn("model_catalog_json", updated)
            self.assertNotIn("[model_providers.custom]", updated)
            self.assertIn("[features]", updated)


if __name__ == "__main__":
    unittest.main()
