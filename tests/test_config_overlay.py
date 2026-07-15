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
    context_guard_status,
    inject_unified_history_config,
    inspect_unified_history_config,
    main as config_overlay_main,
    restore_overlay,
    catalog_config_value,
    set_context_guard,
    set_feature_flags,
    strip_section,
    strip_top_level_keys,
    top_level_value,
)
from model_limits import FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE


class DeterministicCompactionReplay:
    """A narrow Codex runtime-config contract replay used by #124.

    Codex App owns the actual compaction operation.  This fixture consumes the
    same generated top-level runtime setting and asserts the sequencing
    contract: an ordinary generation may not leave the scheduler at or above
    the configured threshold before compaction completes.
    """

    def __init__(self, config_text: str):
        raw_limit = top_level_value(config_text, "model_auto_compact_token_limit")
        self.auto_compact_token_limit = int(raw_limit or "0")
        self.events: list[tuple[str, int]] = []
        self.compaction_pending = False

    def submit_ordinary_generation(self, input_tokens: int) -> bool:
        if self.compaction_pending:
            self.events.append(("ordinary_generation_withheld", input_tokens))
            return False
        if input_tokens >= self.auto_compact_token_limit:
            self.compaction_pending = True
            self.events.append(("context_compacted", input_tokens))
            return False
        self.events.append(("ordinary_generation", input_tokens))
        return True

    def complete_compaction(self, compacted_input_tokens: int) -> None:
        self.compaction_pending = False
        self.events.append(("compaction_completed", compacted_input_tokens))


class ConfigOverlayTests(unittest.TestCase):
    def _official_budget_catalog(
        self,
        root: Path,
        *,
        context_window: int = 272_000,
        auto_compact_token_limit: int = 240_000,
    ) -> Path:
        catalog_path = root / "catalog.json"
        catalog_path.write_text(
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-terra",
                            "codex_proxy_metadata": {
                                "provider": "openai",
                                "upstream_name": "official",
                                "official_context_budget": {
                                    "source": "current_direct_official",
                                    "freshness": "fresh",
                                    "model_context_window": context_window,
                                    "effective_context_window_percent": 100,
                                    "effective_context_window": context_window,
                                    "model_auto_compact_token_limit": auto_compact_token_limit,
                                },
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return catalog_path

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
        self.assertIn('model = "gpt-5.5"', cleaned)
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
                'model = "volc/glm-5.2"',
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
            self.assertIn('model = "volc/glm-5.2"', updated)
            self.assertIn('model_provider = "custom"', updated)
            self.assertIn(f"model_catalog_json = '{catalog_path.resolve()}'", updated)
            self.assertIn("[model_providers.custom]", updated)
            self.assertIn("base_url = 'http://127.0.0.1:9099/v1'", updated)
            self.assertIn('wire_api = "responses"', updated)
            self.assertIn("requires_openai_auth = true", updated)
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

    def test_overlay_projects_safe_catalog_budget_across_restart_and_missing_catalog_fallback(self):
        original = "\n".join(
            [
                'model = "gpt-5.6-terra"',
                "model_context_window = 353400",
                "model_auto_compact_token_limit = 300000",
                'model_reasoning_effort = "high"',
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "model-catalogs" / "catalog.json"
            catalog_path.parent.mkdir()
            config_path.write_text(original, encoding="utf-8")
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": "current_direct_official",
                                        "freshness": "fresh",
                                        "model_context_window": 272_000,
                                        "effective_context_window_percent": 100,
                                        "effective_context_window": 272_000,
                                        "model_auto_compact_token_limit": 240_000,
                                    }
                                },
                            },
                            {
                                "slug": "volc/glm-5.2",
                                "codex_proxy_metadata": {
                                    "official_context_budget": {
                                        "model_context_window": 1_000_000,
                                        "model_auto_compact_token_limit": 900_000,
                                    }
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            activated = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 272000", activated)
            self.assertIn("model_auto_compact_token_limit = 240000", activated)
            self.assertEqual(activated.count("model_context_window"), 1)
            self.assertEqual(activated.count("model_auto_compact_token_limit"), 1)
            self.assertLess(240_000, 249_433)

            catalog_path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safe current Official context budget"):
                apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            self.assertEqual(config_path.read_text(encoding="utf-8"), activated)

            restore_overlay(config_path, backup_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_overlay_uses_dynamic_catalog_compaction_when_direct_omits_no_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text('model = "gpt-5.6-terra"\n', encoding="utf-8")
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": "current_direct_official",
                                        "freshness": "fresh",
                                        "model_context_window": 272_000,
                                        "effective_context_window_percent": 95,
                                        "effective_context_window": 258_400,
                                        "model_auto_compact_token_limit": 244_800,
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")

            text = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 272000", text)
            self.assertIn("model_auto_compact_token_limit = 244800", text)
            self.assertLess(244_800, 249_433)

    def test_249433_token_replay_compacts_before_the_next_ordinary_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = self._official_budget_catalog(tmp)
            config_path.write_text(
                "\n".join(
                    [
                        'model = "gpt-5.6-terra"',
                        "model_context_window = 353400",
                        "model_auto_compact_token_limit = 300000",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            replay = DeterministicCompactionReplay(config_path.read_text(encoding="utf-8"))

            self.assertFalse(replay.submit_ordinary_generation(249_433))
            self.assertEqual(replay.events, [("context_compacted", 249_433)])

            self.assertFalse(replay.submit_ordinary_generation(45_514))
            self.assertEqual(
                replay.events,
                [
                    ("context_compacted", 249_433),
                    ("ordinary_generation_withheld", 45_514),
                ],
            )

            replay.complete_compaction(45_514)
            self.assertTrue(replay.submit_ordinary_generation(45_514))
            self.assertEqual(
                replay.events,
                [
                    ("context_compacted", 249_433),
                    ("ordinary_generation_withheld", 45_514),
                    ("compaction_completed", 45_514),
                    ("ordinary_generation", 45_514),
                ],
            )

    def test_overlay_adopts_a_larger_budget_only_from_a_fresh_direct_catalog_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text('model = "gpt-5.6-terra"\n', encoding="utf-8")

            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": "current_direct_official",
                                        "freshness": "fresh",
                                        "model_context_window": 400_000,
                                        "effective_context_window_percent": 100,
                                        "effective_context_window": 400_000,
                                        "model_auto_compact_token_limit": 380_000,
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            self.assertIn("model_context_window = 400000", config_path.read_text(encoding="utf-8"))
            self.assertIn("model_auto_compact_token_limit = 380000", config_path.read_text(encoding="utf-8"))

            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": "current_direct_official",
                                        "freshness": "stale",
                                        "model_context_window": 500_000,
                                        "model_auto_compact_token_limit": 450_000,
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "safe current Official context budget"):
                apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            self.assertIn("model_context_window = 400000", config_path.read_text(encoding="utf-8"))

            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": "degraded_last_known_official",
                                        "freshness": "stale",
                                        "model_context_window": 400_000,
                                        "effective_context_window_percent": 100,
                                        "effective_context_window": 400_000,
                                        "model_auto_compact_token_limit": 380_000,
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            restarted = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 400000", restarted)
            self.assertIn("model_auto_compact_token_limit = 380000", restarted)

    def test_overlay_adopts_fresh_direct_cache_authority_and_rejects_a_stale_expansion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text('model = "gpt-5.6-terra"\n', encoding="utf-8")

            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
                                        "freshness": "fresh",
                                        "model_context_window": 272_000,
                                        "effective_context_window_percent": 95,
                                        "effective_context_window": 258_400,
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            activated = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 272000", activated)
            self.assertIn("model_auto_compact_token_limit = 244800", activated)
            self.assertLess(244_800, 249_433)

            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
                                        "freshness": "stale",
                                        "model_context_window": 400_000,
                                        "effective_context_window_percent": 100,
                                        "effective_context_window": 400_000,
                                        "model_auto_compact_token_limit": 380_000,
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "safe current Official context budget"):
                apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            self.assertEqual(config_path.read_text(encoding="utf-8"), activated)

    def test_overlay_preserves_an_explicit_third_party_context_budget(self):
        original = "\n".join(
            [
                'model = "volc/glm-5.2"',
                "model_context_window = 1000000",
                "model_auto_compact_token_limit = 900000",
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text(original, encoding="utf-8")
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": FRESH_DIRECT_OFFICIAL_CACHE_AUTHORITY_SOURCE,
                                        "freshness": "fresh",
                                        "model_context_window": 272_000,
                                        "effective_context_window_percent": 100,
                                        "effective_context_window": 272_000,
                                        "model_auto_compact_token_limit": 240_000,
                                    },
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            active = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 1000000", active)
            self.assertIn("model_auto_compact_token_limit = 900000", active)

            restore_overlay(config_path, backup_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_overlay_recovers_an_interrupted_official_activation_without_replacing_backup(self):
        original = 'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "high"\n'

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text(original, encoding="utf-8")
            backup_path.write_text(original, encoding="utf-8")
            catalog_path.write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "safe current Official context budget"):
                apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(backup_path.read_text(encoding="utf-8"), original)

            restore_overlay(config_path, backup_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_apply_cli_keeps_openai_account_with_local_gateway_bearer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"

            exit_code = config_overlay_main(
                [
                    "apply",
                    "--config",
                    str(config_path),
                    "--backup",
                    str(backup_path),
                    "--catalog",
                    str(catalog_path),
                    "--base-url",
                    "http://127.0.0.1:9109",
                    "--gateway-key",
                    "local-test-key",
                ]
            )

            generated = config_path.read_text(encoding="utf-8")
            self.assertEqual(exit_code, 0)
            self.assertNotRegex(generated, r"(?m)^model\s*=")
            self.assertIn("requires_openai_auth = true", generated)
            self.assertIn('experimental_bearer_token = "local-test-key"', generated)
            self.assertNotIn("requires_openai_auth = false", generated)

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

    def test_explicit_takeover_restore_recovers_previous_channel_overlay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "beta-backup.toml"
            catalog = tmp / "catalog.json"
            previous = "# BEGIN CODEX PROXY SESSION CONFIG\n# owner = release\n# END CODEX PROXY SESSION CONFIG\n"
            config.write_text(previous, encoding="utf-8")

            apply_overlay(
                config,
                backup,
                catalog,
                "http://127.0.0.1:9109",
                owner="beta",
                takeover=True,
            )
            restore_overlay(config, backup, unified_history=True)

            self.assertEqual(config.read_text(encoding="utf-8"), previous)

    def test_beta_takeover_reapply_disconnect_unifies_unowned_history(self):
        original = b'model = "original"\r\n[features]\r\nfoo = true\r\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "beta-backup.toml"
            catalog = tmp / "catalog.json"
            config.write_bytes(original)

            apply_overlay(config, backup, catalog, "http://127.0.0.1:9109", owner="beta", takeover=True)
            apply_overlay(config, backup, catalog, "http://127.0.0.1:9109", owner="beta")
            restore_overlay(config, backup, unified_history=True)

            restored = config.read_text(encoding="utf-8")
            self.assertIn('model = "original"', restored)
            self.assertIn('model_provider = "custom"', restored)
            self.assertIn('[model_providers.custom]', restored)
            self.assertIn('name = "OpenAI"', restored)
            self.assertNotIn("base_url", restored)
            self.assertFalse(backup.exists())

    def test_beta_takeover_reapply_disconnect_restores_stable_owner_bytes_exactly(self):
        original = (
            b"# BEGIN CODEX PROXY SESSION CONFIG\n"
            b"# owner = release\n"
            b"# END CODEX PROXY SESSION CONFIG\n"
            b'model_reasoning_effort = "high"\n'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "beta-backup.toml"
            catalog = tmp / "catalog.json"
            config.write_bytes(original)

            apply_overlay(config, backup, catalog, "http://127.0.0.1:9109", owner="beta", takeover=True)
            apply_overlay(config, backup, catalog, "http://127.0.0.1:9109", owner="beta")
            restore_overlay(config, backup, unified_history=True)

            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(backup.exists())

    def test_same_owner_force_with_missing_backup_does_not_create_takeover_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "release-backup.toml"
            catalog = tmp / "catalog.json"

            apply_overlay(config, backup, catalog, "http://127.0.0.1:9099", owner="release")
            backup.unlink()
            apply_overlay(
                config,
                backup,
                catalog,
                "http://127.0.0.1:9099",
                owner="release",
                takeover=True,
            )

            self.assertEqual(list(tmp.glob("*.takeover.json")), [])
            restore_overlay(config, backup, unified_history=True)
            restored = config.read_text(encoding="utf-8")
            self.assertIn('name = "OpenAI"', restored)
            self.assertNotIn('name = "Codex Proxy"', restored)
            self.assertNotIn("base_url", restored)

    def test_restore_ignores_preexisting_same_owner_takeover_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config = tmp / "config.toml"
            backup = tmp / "release-backup.toml"
            metadata = tmp / "release-backup.toml.takeover.json"
            config.write_text(
                "# BEGIN CODEX PROXY SESSION CONFIG\n# owner = release\n# END CODEX PROXY SESSION CONFIG\n",
                encoding="utf-8",
            )
            backup.write_text(
                "# BEGIN CODEX PROXY SESSION CONFIG\n# owner = release\n# END CODEX PROXY SESSION CONFIG\n",
                encoding="utf-8",
            )
            metadata.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "takeover_owner": "release",
                        "original_owner": "release",
                    }
                ),
                encoding="utf-8",
            )

            status = restore_overlay(config, backup, unified_history=True)
            restored = config.read_text(encoding="utf-8")

            self.assertNotEqual(status, "restored_takeover_backup")
            self.assertIn('name = "OpenAI"', restored)
            self.assertFalse(metadata.exists())

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

    def test_same_channel_restore_reconciles_unified_official_history(self):
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
            self.assertIn('model_reasoning_effort = "high"', updated)

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

    def test_context_guard_updates_live_and_overlay_backup_then_restores_previous_values(self):
        original = "\n".join(
            [
                "model_context_window = 400000",
                "model_auto_compact_token_limit = 360000",
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
            state_path = tmp / "context-guard-state.json"
            config_path.write_text(original, encoding="utf-8")
            backup_path.write_text(original, encoding="utf-8")
            catalog_path = self._official_budget_catalog(tmp)

            enabled = set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=True,
                catalog_path=catalog_path,
            )

            self.assertTrue(enabled["enabled"])
            self.assertEqual(enabled["model_context_window"], 272_000)
            self.assertEqual(
                enabled["model_auto_compact_token_limit"],
                240_000,
            )
            for path in (config_path, backup_path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("model_context_window = 272000", text)
                self.assertIn(
                    "model_auto_compact_token_limit = 240000",
                    text,
                )
                self.assertIn('model_reasoning_effort = "high"', text)
                self.assertIn("[features]", text)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            for target in ("config", "backup"):
                self.assertEqual(state[target]["previous"]["model_context_window"], "400000")
                self.assertEqual(
                    state[target]["previous"]["model_auto_compact_token_limit"],
                    "360000",
                )

            disabled = set_context_guard(config_path, backup_path, state_path, enabled=False)

            self.assertFalse(disabled["enabled"])
            self.assertFalse(state_path.exists())
            for path in (config_path, backup_path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("model_context_window = 400000", text)
                self.assertIn("model_auto_compact_token_limit = 360000", text)
                self.assertIn('model_reasoning_effort = "high"', text)

    def test_context_guard_disable_does_not_restore_an_unsafe_official_override(self):
        original = "\n".join(
            [
                'model = "gpt-5.6-terra"',
                "model_context_window = 400000",
                "model_auto_compact_token_limit = 360000",
                "",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            state_path = tmp / "context-guard-state.json"
            catalog_path = self._official_budget_catalog(tmp)
            config_path.write_text(original, encoding="utf-8")
            backup_path.write_text(original, encoding="utf-8")

            set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=True,
                catalog_path=catalog_path,
            )
            disabled = set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=False,
                catalog_path=catalog_path,
            )

            self.assertFalse(disabled["enabled"])
            for path in (config_path, backup_path):
                text = path.read_text(encoding="utf-8")
                self.assertIn("model_context_window = 272000", text)
                self.assertIn("model_auto_compact_token_limit = 240000", text)
                self.assertNotIn("model_context_window = 400000", text)
                self.assertNotIn("model_auto_compact_token_limit = 360000", text)

    def test_context_guard_disable_keeps_a_third_party_backup_unchanged(self):
        official = (
            'model = "gpt-5.6-terra"\n'
            "model_context_window = 400000\n"
            "model_auto_compact_token_limit = 360000\n"
        )
        third_party_backup = (
            'model = "volc/glm-5.2"\n'
            "model_context_window = 1000000\n"
            "model_auto_compact_token_limit = 900000\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            state_path = tmp / "context-guard-state.json"
            catalog_path = self._official_budget_catalog(tmp)
            config_path.write_text(official, encoding="utf-8")
            backup_path.write_text(third_party_backup, encoding="utf-8")

            set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=True,
                catalog_path=catalog_path,
            )
            set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=False,
                catalog_path=catalog_path,
            )

            self.assertIn("model_context_window = 272000", config_path.read_text(encoding="utf-8"))
            restored_backup = backup_path.read_text(encoding="utf-8")
            self.assertIn('model = "volc/glm-5.2"', restored_backup)
            self.assertIn("model_context_window = 1000000", restored_backup)
            self.assertIn("model_auto_compact_token_limit = 900000", restored_backup)

    def test_context_guard_disable_removes_managed_values_when_no_previous_values_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            state_path = tmp / "context-guard-state.json"
            config_path.write_text("[features]\nhooks = true\n", encoding="utf-8")
            catalog_path = self._official_budget_catalog(tmp)

            set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=True,
                catalog_path=catalog_path,
            )
            self.assertTrue(context_guard_status(config_path, state_path)["enabled"])

            set_context_guard(config_path, backup_path, state_path, enabled=False)
            text = config_path.read_text(encoding="utf-8")
            self.assertNotIn("model_context_window", text)
            self.assertNotIn("model_auto_compact_token_limit", text)
            self.assertIn("[features]", text)
            self.assertIn("hooks = true", text)

    def test_context_guard_restores_distinct_live_and_backup_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            state_path = tmp / "context-guard-state.json"
            config_path.write_text(
                "model_context_window = 500000\n"
                "model_auto_compact_token_limit = 450000\n",
                encoding="utf-8",
            )
            backup_path.write_text(
                "model_context_window = 400000\n"
                "model_auto_compact_token_limit = 360000\n",
                encoding="utf-8",
            )
            catalog_path = self._official_budget_catalog(tmp)

            set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=True,
                catalog_path=catalog_path,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["config"]["previous"]["model_context_window"], "500000")
            self.assertEqual(state["backup"]["previous"]["model_context_window"], "400000")

            set_context_guard(config_path, backup_path, state_path, enabled=False)

            self.assertIn(
                "model_context_window = 500000",
                config_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "model_auto_compact_token_limit = 450000",
                config_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "model_context_window = 400000",
                backup_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "model_auto_compact_token_limit = 360000",
                backup_path.read_text(encoding="utf-8"),
            )

    def test_context_guard_disable_preserves_a_value_changed_after_enable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            state_path = tmp / "context-guard-state.json"
            config_path.write_text("model_context_window = 500000\n", encoding="utf-8")
            catalog_path = self._official_budget_catalog(tmp)

            set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=True,
                catalog_path=catalog_path,
            )
            changed = config_path.read_text(encoding="utf-8").replace(
                "model_context_window = 272000",
                "model_context_window = 600000",
            )
            config_path.write_text(changed, encoding="utf-8")

            set_context_guard(config_path, backup_path, state_path, enabled=False)
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 600000", text)
            self.assertNotIn("model_auto_compact_token_limit", text)

    def test_context_guard_adopts_preexisting_managed_values_and_can_fully_disable_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            state_path = tmp / "context-guard-state.json"
            config_path.write_text(
                "\n".join(
                    [
                        "model_context_window = 272000",
                        "model_auto_compact_token_limit = 240000",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            catalog_path = self._official_budget_catalog(tmp)

            set_context_guard(
                config_path,
                backup_path,
                state_path,
                enabled=True,
                catalog_path=catalog_path,
            )
            set_context_guard(config_path, backup_path, state_path, enabled=False)

            self.assertFalse(context_guard_status(config_path, state_path)["enabled"])
            text = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 272000", text)
            self.assertIn("model_auto_compact_token_limit = 240000", text)


if __name__ == "__main__":
    unittest.main()
