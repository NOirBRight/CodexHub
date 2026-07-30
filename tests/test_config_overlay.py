from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

import config_overlay
from config_overlay import (
    MARKER_BEGIN,
    _selected_official_context_budget,
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


RUST_0_145_AGENTS_CONFIG = """\
model = "ollama-cloud/glm-5.2"
model_provider = "openai"
model_reasoning_effort = "xhigh"

[agents]
enabled = true
max_concurrent_threads_per_session = 7
default_subagent_model = "gpt-5.6-terra"
default_subagent_reasoning_effort = "high"
future_scheduler = { strategy = "breadth", burst_limit = 11 }

[agents.researcher]
description = "Research-focused role."
config_file = "./agents/researcher.toml"
nickname_candidates = ["Herodotus", "Ibn Battuta"]

[agents.reviewer]
description = "Review-focused role."
config_file = "./agents/reviewer.toml"

[features]
multi_agent_v2 = { enabled = false, max_concurrent_threads_per_session = 5, tool_namespace = "team_tools" }
hooks = true
"""


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
    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        source_path = str(Path(config_overlay.__file__).parent)
        inherited_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            source_path
            if not inherited_pythonpath
            else source_path + os.pathsep + inherited_pythonpath
        )
        return env

    def _start_lifecycle_lock_holder(self, config_path: Path) -> subprocess.Popen[str]:
        holder_script = "\n".join(
            [
                "import sys",
                "from pathlib import Path",
                "from atomic_io import file_lock_for",
                "from config_overlay import overlay_lifecycle_lock_target",
                "config_path = Path(sys.argv[1])",
                "with file_lock_for(overlay_lifecycle_lock_target(config_path)):",
                '    print("held", flush=True)',
                "    sys.stdin.readline()",
            ]
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script, str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._subprocess_env(),
        )
        assert holder.stdout is not None
        if holder.stdout.readline().strip() != "held":
            assert holder.stderr is not None
            error = holder.stderr.read()
            holder.wait(timeout=5)
            self.fail(f"lifecycle lock holder failed to start: {error}")
        return holder

    def _release_lifecycle_lock_holder(self, holder: subprocess.Popen[str]) -> None:
        assert holder.stdin is not None
        assert holder.stderr is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder.stdin.close()
        holder.wait(timeout=5)
        self.assertEqual(holder.returncode, 0, holder.stderr.read())

    def _start_traced_config_overlay_process(
        self,
        config_path: Path,
        arguments: list[str],
    ) -> tuple[subprocess.Popen[str], str]:
        traced_cli_script = "\n".join(
            [
                "import contextlib",
                "import sys",
                "from pathlib import Path",
                "import config_overlay",
                "expected = config_overlay.overlay_lifecycle_lock_target(Path(sys.argv[1]))",
                "real_file_lock_for = config_overlay.file_lock_for",
                "@contextlib.contextmanager",
                "def traced_file_lock_for(path):",
                "    if Path(path).resolve(strict=False) == expected.resolve(strict=False):",
                '        print("lifecycle-attempt", flush=True)',
                "    with real_file_lock_for(path) as verify_namespace:",
                "        yield verify_namespace",
                "config_overlay.file_lock_for = traced_file_lock_for",
                "raise SystemExit(config_overlay.main(sys.argv[2:]))",
            ]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                traced_cli_script,
                str(config_path),
                *arguments,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._subprocess_env(),
        )
        assert process.stdout is not None
        return process, process.stdout.readline().strip()

    def _start_paused_config_overlay_writer(
        self,
        config_path: Path,
        arguments: list[str],
    ) -> subprocess.Popen[str]:
        paused_cli_script = "\n".join(
            [
                "import contextlib",
                "import sys",
                "from pathlib import Path",
                "import config_overlay",
                "expected = config_overlay.overlay_lifecycle_lock_target(Path(sys.argv[1]))",
                "real_file_lock_for = config_overlay.file_lock_for",
                "@contextlib.contextmanager",
                "def paused_file_lock_for(path):",
                "    with real_file_lock_for(path) as verify_namespace:",
                "        if Path(path).resolve(strict=False) == expected.resolve(strict=False):",
                '            print("writer-held", flush=True)',
                "            sys.stdin.readline()",
                "        yield verify_namespace",
                "config_overlay.file_lock_for = paused_file_lock_for",
                "raise SystemExit(config_overlay.main(sys.argv[2:]))",
            ]
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                paused_cli_script,
                str(config_path),
                *arguments,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._subprocess_env(),
        )
        assert process.stdout is not None
        if process.stdout.readline().strip() != "writer-held":
            assert process.stderr is not None
            error = process.stderr.read()
            process.wait(timeout=5)
            self.fail(f"managed config writer failed to acquire lifecycle lock: {error}")
        return process

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

    def _apply_interrupted_takeover(
        self,
        config_path: Path,
        backup_path: Path,
        catalog_path: Path,
        *,
        owner: str,
        base_url: str,
    ) -> None:
        real_atomic_write = config_overlay.atomic_write_text

        def interrupt_live_config_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
            if path == config_path:
                raise OSError(f"simulated interrupted {owner} config update")
            real_atomic_write(path, text, encoding=encoding)

        with patch("config_overlay.atomic_write_text", interrupt_live_config_write):
            with self.assertRaisesRegex(OSError, f"interrupted {owner} config update"):
                apply_overlay(
                    config_path,
                    backup_path,
                    catalog_path,
                    base_url,
                    owner=owner,
                    takeover=True,
                )

    def _stable_with_interrupted_beta_takeover(
        self,
        root: Path,
    ) -> tuple[Path, Path, Path, bytes]:
        config_path = root / "config.toml"
        stable_backup_path = root / "stable.backup.toml"
        beta_backup_path = root / "beta.backup.toml"
        catalog_path = root / "catalog.json"
        config_path.write_text(RUST_0_145_AGENTS_CONFIG, encoding="utf-8")
        apply_overlay(
            config_path,
            stable_backup_path,
            catalog_path,
            "http://127.0.0.1:9099",
            owner="release",
        )
        stable_active = config_path.read_bytes()
        self._apply_interrupted_takeover(
            config_path,
            beta_backup_path,
            catalog_path,
            owner="beta",
            base_url="http://127.0.0.1:9109",
        )
        return config_path, beta_backup_path, catalog_path, stable_active

    def _unowned_with_active_beta_takeover(
        self,
        root: Path,
    ) -> tuple[Path, Path, bytes, bytes, Path]:
        config_path = root / "config.toml"
        backup_path = root / "beta.backup.toml"
        catalog_path = root / "catalog.json"
        original = RUST_0_145_AGENTS_CONFIG.encode()
        config_path.write_bytes(original)
        apply_overlay(
            config_path,
            backup_path,
            catalog_path,
            "http://127.0.0.1:9109",
            owner="beta",
            takeover=True,
        )
        return (
            config_path,
            backup_path,
            original,
            config_path.read_bytes(),
            config_overlay.takeover_metadata_path(backup_path),
        )

    def _unowned_with_pending_unified_cleanup(
        self,
        root: Path,
    ) -> tuple[Path, Path, Path]:
        config_path, backup_path, _, _, metadata_path = (
            self._unowned_with_active_beta_takeover(root)
        )
        real_atomic_write = config_overlay.atomic_write_text

        def fail_final_publish(path: Path, text: str, *, encoding: str = "utf-8") -> None:
            if path == config_path:
                raise OSError("simulated pending unified final publication")
            real_atomic_write(path, text, encoding=encoding)

        with patch("config_overlay.atomic_write_text", fail_final_publish):
            with self.assertRaisesRegex(OSError, "pending unified final publication"):
                restore_overlay(config_path, backup_path, unified_history=True)
        return config_path, backup_path, metadata_path

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

    def test_selected_official_context_budget_prefers_active_model_over_unrelated_default(self):
        """Active task-model budget wins; do not fall back to an unrelated Official default.

        Regression for #157: the resolver must match the selected model exactly
        and must not fall back to the first persisted Official budget when the
        active model is a non-gpt task export or when no model is selected.
        """

        def make_budget(
            *,
            context_window: int,
            effective_context_window: int,
            auto_compact_token_limit: int,
            source: str = "current_direct_official",
            freshness: str = "fresh",
        ) -> dict[str, object]:
            return {
                "source": source,
                "freshness": freshness,
                "model_context_window": context_window,
                "effective_context_window_percent": (
                    effective_context_window * 100 // context_window
                ),
                "effective_context_window": effective_context_window,
                "model_auto_compact_token_limit": auto_compact_token_limit,
            }

        official_budget = make_budget(
            context_window=272_000,
            effective_context_window=258_400,
            auto_compact_token_limit=244_800,
        )
        task_budget = make_budget(
            context_window=1_000_000,
            effective_context_window=950_000,
            auto_compact_token_limit=900_000,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            catalog_path = tmp / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-terra",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": official_budget,
                                },
                            },
                            {
                                "slug": "volc/glm-5.2",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": task_budget,
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cases = [
                {
                    "selected": "volc/glm-5.2",
                    "expected": (1_000_000, 900_000),
                    "description": "task model exports 950k effective",
                },
                {
                    "selected": "gpt-5.6-terra",
                    "expected": (272_000, 244_800),
                    "description": "active Official remains 258400 effective",
                },
                {
                    "selected": "openai/gpt-5.6-terra",
                    "expected": (272_000, 244_800),
                    "description": "openai/ prefix normalizes to Official budget",
                },
                {
                    "selected": "unknown/model",
                    "expected": None,
                    "description": "unknown model returns None instead of arbitrary default",
                },
                {
                    "selected": None,
                    "expected": (272_000, 244_800),
                    "description": "no selection falls back to first persisted Official budget",
                },
            ]

            for case in cases:
                with self.subTest(selected=case["selected"], msg=case["description"]):
                    self.assertEqual(
                        _selected_official_context_budget(catalog_path, case["selected"]),
                        case["expected"],
                    )

    def test_overlay_uses_active_task_model_budget_over_unrelated_official_default(self):
        """End-to-end overlay writes the active task-model budget, not an unrelated Official default."""

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text('model = "volc/glm-5.2"\n', encoding="utf-8")
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
                            },
                            {
                                "slug": "volc/glm-5.2",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": "current_direct_official",
                                        "freshness": "fresh",
                                        "model_context_window": 1_000_000,
                                        "effective_context_window_percent": 95,
                                        "effective_context_window": 950_000,
                                        "model_auto_compact_token_limit": 900_000,
                                    },
                                },
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            apply_overlay(config_path, backup_path, catalog_path, "http://127.0.0.1:9099")
            activated = config_path.read_text(encoding="utf-8")
            self.assertIn("model_context_window = 1000000", activated)
            self.assertIn("model_auto_compact_token_limit = 900000", activated)
            self.assertEqual(activated.count("model_context_window"), 1)
            self.assertEqual(activated.count("model_auto_compact_token_limit"), 1)

    def test_overlay_uses_active_official_budget_when_official_model_selected(self):
        """Active Official model keeps its own budget when a task model is also in the catalog."""

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
                            },
                            {
                                "slug": "volc/glm-5.2",
                                "codex_proxy_metadata": {
                                    "provider": "openai",
                                    "upstream_name": "official",
                                    "official_context_budget": {
                                        "source": "current_direct_official",
                                        "freshness": "fresh",
                                        "model_context_window": 1_000_000,
                                        "effective_context_window_percent": 95,
                                        "effective_context_window": 950_000,
                                        "model_auto_compact_token_limit": 900_000,
                                    },
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
            self.assertIn("model_auto_compact_token_limit = 244800", activated)
            self.assertEqual(activated.count("model_context_window"), 1)
            self.assertEqual(activated.count("model_auto_compact_token_limit"), 1)

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

    def test_interrupted_beta_takeover_leaves_stable_agents_config_and_restore_chain_exact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            stable_backup_path = tmp / "stable.backup.toml"
            beta_backup_path = tmp / "beta.backup.toml"
            catalog_path = tmp / "catalog.json"
            original = RUST_0_145_AGENTS_CONFIG.replace("\n", "\r\n").encode()
            expected = tomllib.loads(RUST_0_145_AGENTS_CONFIG)
            config_path.write_bytes(original)

            apply_overlay(
                config_path,
                stable_backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
                owner="release",
            )
            stable_active = config_path.read_bytes()
            stable_semantics = tomllib.loads(stable_active.decode())
            self.assertEqual(stable_semantics["agents"], expected["agents"])
            self.assertEqual(
                stable_semantics["features"]["multi_agent_v2"],
                expected["features"]["multi_agent_v2"],
            )

            real_atomic_write = config_overlay.atomic_write_text

            def interrupt_live_config_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
                if path == config_path:
                    raise OSError("simulated interrupted beta config update")
                real_atomic_write(path, text, encoding=encoding)

            with patch("config_overlay.atomic_write_text", interrupt_live_config_write):
                with self.assertRaisesRegex(OSError, "interrupted beta config update"):
                    apply_overlay(
                        config_path,
                        beta_backup_path,
                        catalog_path,
                        "http://127.0.0.1:9109",
                        owner="beta",
                        takeover=True,
                    )

            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertEqual(beta_backup_path.read_bytes(), stable_active)
            self.assertTrue(config_overlay.takeover_metadata_path(beta_backup_path).exists())

            status = restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(status, "interrupted_takeover_discarded")
            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertFalse(beta_backup_path.exists())
            self.assertFalse(config_overlay.takeover_metadata_path(beta_backup_path).exists())

            restore_overlay(config_path, stable_backup_path)
            self.assertEqual(config_path.read_bytes(), original)

    def test_interrupted_takeover_with_missing_live_config_retains_recovery_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "beta.backup.toml"
            catalog_path = tmp / "catalog.json"
            original = RUST_0_145_AGENTS_CONFIG.encode()
            config_path.write_bytes(original)
            self._apply_interrupted_takeover(
                config_path,
                backup_path,
                catalog_path,
                owner="beta",
                base_url="http://127.0.0.1:9109",
            )

            metadata_path = config_overlay.takeover_metadata_path(backup_path)
            config_path.unlink()

            with self.assertRaisesRegex(RuntimeError, "live config is missing or diverged"):
                restore_overlay(config_path, backup_path, unified_history=True)

            self.assertFalse(config_path.exists())
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertTrue(metadata_path.exists())

    def test_interrupted_takeover_with_same_owner_diverged_live_bytes_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, beta_backup_path, _, stable_active_bytes = (
                self._stable_with_interrupted_beta_takeover(tmp)
            )
            stable_active = stable_active_bytes.decode()

            diverged = stable_active + "# user edit after interrupted takeover\n"
            config_path.write_text(diverged, encoding="utf-8")
            metadata_path = config_overlay.takeover_metadata_path(beta_backup_path)

            with self.assertRaisesRegex(RuntimeError, "live config is missing or diverged"):
                restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(config_path.read_text(encoding="utf-8"), diverged)
            self.assertEqual(beta_backup_path.read_text(encoding="utf-8"), stable_active)
            self.assertTrue(metadata_path.exists())

    def test_interrupted_restore_serializes_against_concurrent_takeover_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, beta_backup_path, catalog_path, _ = (
                self._stable_with_interrupted_beta_takeover(tmp)
            )

            restore_classified = threading.Event()
            release_restore = threading.Event()
            retry_attempted = threading.Event()
            retry_entered_transaction = threading.Event()
            thread_errors: list[BaseException] = []
            real_read_takeover_metadata = config_overlay.read_takeover_metadata
            real_apply_overlay_locked = config_overlay._apply_overlay_locked

            def pause_after_metadata_read(
                backup_path: Path,
            ) -> config_overlay.TakeoverMetadataRead:
                result = real_read_takeover_metadata(backup_path)
                restore_classified.set()
                if not release_restore.wait(timeout=5):
                    raise TimeoutError("test did not release restore classification")
                return result

            def record_retry_transaction_entry(*args: object, **kwargs: object) -> None:
                retry_entered_transaction.set()
                real_apply_overlay_locked(*args, **kwargs)

            def run_restore() -> None:
                try:
                    restore_overlay(config_path, beta_backup_path, unified_history=True)
                except BaseException as exc:
                    thread_errors.append(exc)

            def retry_takeover() -> None:
                try:
                    retry_attempted.set()
                    apply_overlay(
                        config_path,
                        beta_backup_path,
                        catalog_path,
                        "http://127.0.0.1:9109",
                        owner="beta",
                        takeover=True,
                    )
                except BaseException as exc:
                    thread_errors.append(exc)

            with (
                patch("config_overlay.read_takeover_metadata", pause_after_metadata_read),
                patch("config_overlay._apply_overlay_locked", record_retry_transaction_entry),
            ):
                restore_thread = threading.Thread(target=run_restore)
                retry_thread = threading.Thread(target=retry_takeover)
                restore_thread.start()
                self.assertTrue(restore_classified.wait(timeout=5))
                retry_thread.start()
                try:
                    self.assertTrue(retry_attempted.wait(timeout=5))
                    self.assertFalse(
                        retry_entered_transaction.wait(timeout=0.25),
                        "takeover retry entered while restore held the lifecycle transaction",
                    )
                finally:
                    release_restore.set()
                    restore_thread.join(timeout=5)
                    retry_thread.join(timeout=5)

            self.assertFalse(restore_thread.is_alive())
            self.assertFalse(retry_thread.is_alive())
            self.assertEqual(thread_errors, [])
            self.assertEqual(config_overlay.overlay_owner(config_path.read_text(encoding="utf-8")), "beta")
            self.assertTrue(beta_backup_path.exists())
            self.assertTrue(config_overlay.takeover_metadata_path(beta_backup_path).exists())

    def test_lifecycle_lock_target_canonicalizes_path_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            alias_parent = tmp / "alias"
            alias_parent.mkdir()
            config_path = tmp / "config.toml"
            alias_path = alias_parent / ".." / "config.toml"

            self.assertEqual(
                config_overlay.overlay_lifecycle_lock_target(config_path),
                config_overlay.overlay_lifecycle_lock_target(alias_path),
            )

    def test_managed_writers_reject_hardlink_and_state_path_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = self._official_budget_catalog(tmp)
            original = RUST_0_145_AGENTS_CONFIG.encode()
            config_path.write_bytes(original)
            os.link(config_path, backup_path)

            with self.assertRaisesRegex(ValueError, "config aliases backup"):
                apply_overlay(
                    config_path,
                    backup_path,
                    catalog_path,
                    "http://127.0.0.1:9099",
                )
            self.assertEqual(config_path.read_bytes(), original)
            self.assertEqual(backup_path.read_bytes(), original)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = self._official_budget_catalog(tmp)
            original = b'model = "gpt-5.6-terra"\n'
            config_path.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "config aliases context guard state"):
                set_context_guard(
                    config_path,
                    backup_path,
                    config_path,
                    enabled=True,
                    catalog_path=catalog_path,
                )
            self.assertEqual(config_path.read_bytes(), original)
            self.assertFalse(backup_path.exists())

    def test_config_writers_serialize_across_processes_on_canonical_lifecycle_lock(self):
        for operation in ("apply", "restore", "context_guard"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                alias_parent = tmp / "alias"
                alias_parent.mkdir()
                config_path = tmp / "config.toml"
                config_alias = alias_parent / ".." / "config.toml"
                backup_path = tmp / "config.backup.toml"
                state_path = tmp / "context-guard-state.json"
                catalog_path = tmp / "catalog.json"
                original = RUST_0_145_AGENTS_CONFIG.encode()

                if operation == "apply":
                    config_path.write_bytes(original)
                    arguments = [
                        "apply",
                        "--config",
                        str(config_path),
                        "--backup",
                        str(backup_path),
                        "--base-url",
                        "http://127.0.0.1:9099",
                    ]
                elif operation == "restore":
                    config_path.write_bytes(original)
                    apply_overlay(
                        config_path,
                        backup_path,
                        None,
                        "http://127.0.0.1:9099",
                    )
                    arguments = [
                        "restore",
                        "--config",
                        str(config_path),
                        "--backup",
                        str(backup_path),
                    ]
                else:
                    config_path.write_text('model = "gpt-5.6-terra"\n', encoding="utf-8")
                    catalog_path = self._official_budget_catalog(tmp)
                    arguments = [
                        "context-guard-set",
                        "--config",
                        str(config_path),
                        "--backup",
                        str(backup_path),
                        "--state",
                        str(state_path),
                        "--catalog",
                        str(catalog_path),
                        "--enabled",
                        "true",
                    ]

                holder = self._start_lifecycle_lock_holder(config_alias)
                contender: subprocess.Popen[str] | None = None
                try:
                    contender, first_line = self._start_traced_config_overlay_process(
                        config_path,
                        arguments,
                    )
                    self.assertEqual(first_line, "lifecycle-attempt")
                    time.sleep(0.25)
                    self.assertIsNone(
                        contender.poll(),
                        f"{operation} bypassed the lifecycle lock",
                    )
                    self._release_lifecycle_lock_holder(holder)
                    stdout, stderr = contender.communicate(timeout=15)
                    self.assertEqual(
                        contender.returncode,
                        0,
                        f"{operation} failed after lock release: {stdout}\n{stderr}",
                    )
                finally:
                    if contender is not None and contender.poll() is None:
                        contender.kill()
                        contender.communicate(timeout=5)
                    if holder.poll() is None:
                        holder.kill()
                        holder.communicate(timeout=5)

                if operation == "apply":
                    self.assertEqual(
                        config_overlay.overlay_owner(
                            config_path.read_text(encoding="utf-8")
                        ),
                        "release",
                    )
                    self.assertEqual(backup_path.read_bytes(), original)
                elif operation == "restore":
                    self.assertEqual(config_path.read_bytes(), original)
                    self.assertFalse(backup_path.exists())
                else:
                    guarded = config_path.read_text(encoding="utf-8")
                    self.assertIn("model_context_window = 272000", guarded)
                    self.assertIn(
                        "model_auto_compact_token_limit = 240000",
                        guarded,
                    )
                    self.assertTrue(state_path.exists())

    def test_lifecycle_lock_recovers_after_holder_process_is_killed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            alias_parent = tmp / "alias"
            alias_parent.mkdir()
            config_path = tmp / "config.toml"
            config_alias = alias_parent / ".." / "config.toml"
            backup_path = tmp / "config.backup.toml"
            config_path.write_text(RUST_0_145_AGENTS_CONFIG, encoding="utf-8")
            holder = self._start_lifecycle_lock_holder(config_alias)

            holder.kill()
            holder.communicate(timeout=5)

            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(config_overlay.__file__)),
                    "apply",
                    "--config",
                    str(config_path),
                    "--backup",
                    str(backup_path),
                    "--base-url",
                    "http://127.0.0.1:9099",
                ],
                capture_output=True,
                text=True,
                env=self._subprocess_env(),
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                config_overlay.overlay_owner(config_path.read_text(encoding="utf-8")),
                "release",
            )
            self.assertTrue(backup_path.exists())

    def test_actual_apply_and_context_guard_subprocesses_serialize_as_writers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            alias_parent = tmp / "alias"
            alias_parent.mkdir()
            config_path = tmp / "config.toml"
            config_alias = alias_parent / ".." / "config.toml"
            backup_path = tmp / "config.backup.toml"
            state_path = tmp / "context-guard-state.json"
            catalog_path = self._official_budget_catalog(tmp)
            config_path.write_text(
                RUST_0_145_AGENTS_CONFIG.replace(
                    'model = "ollama-cloud/glm-5.2"',
                    'model = "gpt-5.6-terra"',
                ),
                encoding="utf-8",
            )
            apply_arguments = [
                "apply",
                "--config",
                str(config_alias),
                "--backup",
                str(backup_path),
                "--catalog",
                str(catalog_path),
                "--base-url",
                "http://127.0.0.1:9099",
            ]
            context_arguments = [
                "context-guard-set",
                "--config",
                str(config_path),
                "--backup",
                str(backup_path),
                "--state",
                str(state_path),
                "--catalog",
                str(catalog_path),
                "--enabled",
                "true",
            ]

            first_writer = self._start_paused_config_overlay_writer(
                config_alias,
                apply_arguments,
            )
            second_writer: subprocess.Popen[str] | None = None
            try:
                second_writer, first_line = self._start_traced_config_overlay_process(
                    config_path,
                    context_arguments,
                )
                self.assertEqual(first_line, "lifecycle-attempt")
                time.sleep(0.25)
                self.assertIsNone(
                    second_writer.poll(),
                    "context guard writer bypassed the active apply transaction",
                )
                self._release_lifecycle_lock_holder(first_writer)
                stdout, stderr = second_writer.communicate(timeout=15)
                self.assertEqual(second_writer.returncode, 0, f"{stdout}\n{stderr}")
            finally:
                if first_writer.poll() is None:
                    first_writer.kill()
                    first_writer.communicate(timeout=5)
                if second_writer is not None and second_writer.poll() is None:
                    second_writer.kill()
                    second_writer.communicate(timeout=5)

            for path in (config_path, backup_path):
                guarded = path.read_text(encoding="utf-8")
                self.assertIn("model_context_window = 272000", guarded)
                self.assertIn("model_auto_compact_token_limit = 240000", guarded)
            self.assertTrue(state_path.exists())

    def test_actual_restore_subprocess_recovers_journal_after_contending_writer_is_killed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            alias_parent = tmp / "alias"
            alias_parent.mkdir()
            config_path, backup_path, metadata_path = (
                self._unowned_with_pending_unified_cleanup(tmp)
            )
            config_alias = alias_parent / ".." / "config.toml"
            restore_alias_arguments = [
                "restore",
                "--config",
                str(config_alias),
                "--backup",
                str(backup_path),
                "--unified-history",
            ]
            restore_arguments = [
                "restore",
                "--config",
                str(config_path),
                "--backup",
                str(backup_path),
                "--unified-history",
            ]

            killed_writer = self._start_paused_config_overlay_writer(
                config_alias,
                restore_alias_arguments,
            )
            recovering_writer: subprocess.Popen[str] | None = None
            try:
                recovering_writer, first_line = self._start_traced_config_overlay_process(
                    config_path,
                    restore_arguments,
                )
                self.assertEqual(first_line, "lifecycle-attempt")
                time.sleep(0.25)
                self.assertIsNone(
                    recovering_writer.poll(),
                    "second restore bypassed the first restore transaction",
                )
                killed_writer.kill()
                killed_writer.communicate(timeout=5)
                stdout, stderr = recovering_writer.communicate(timeout=15)
                self.assertEqual(recovering_writer.returncode, 0, f"{stdout}\n{stderr}")
                self.assertIn("unified_history=injected", stdout)
            finally:
                if killed_writer.poll() is None:
                    killed_writer.kill()
                    killed_writer.communicate(timeout=5)
                if recovering_writer is not None and recovering_writer.poll() is None:
                    recovering_writer.kill()
                    recovering_writer.communicate(timeout=5)

            restored = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(restored["model_provider"], "custom")
            self.assertEqual(restored["model_providers"]["custom"]["name"], "OpenAI")
            self.assertFalse(backup_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_interrupted_takeover_sidecar_delete_failure_resumes_without_rewriting_live_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, beta_backup_path, _, stable_active = (
                self._stable_with_interrupted_beta_takeover(tmp)
            )

            metadata_path = config_overlay.takeover_metadata_path(beta_backup_path)
            real_unlink = Path.unlink

            def fail_sidecar_delete(path: Path, *args: object, **kwargs: object) -> None:
                if path == metadata_path:
                    raise OSError("simulated sidecar delete failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_sidecar_delete):
                with self.assertRaisesRegex(OSError, "sidecar delete failure"):
                    restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertFalse(beta_backup_path.exists())
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(status, "interrupted_takeover_discarded")
            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertFalse(metadata_path.exists())

    def test_interrupted_takeover_backup_delete_failure_retains_artifacts_for_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, beta_backup_path, _, stable_active = (
                self._stable_with_interrupted_beta_takeover(tmp)
            )

            metadata_path = config_overlay.takeover_metadata_path(beta_backup_path)
            real_unlink = Path.unlink

            def fail_backup_delete(path: Path, *args: object, **kwargs: object) -> None:
                if path == beta_backup_path:
                    raise OSError("simulated backup delete failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_backup_delete):
                with self.assertRaisesRegex(OSError, "backup delete failure"):
                    restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertTrue(beta_backup_path.exists())
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(status, "interrupted_takeover_discarded")
            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertFalse(beta_backup_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_interrupted_takeover_cleanup_journal_write_failure_retains_original_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, beta_backup_path, _, stable_active = (
                self._stable_with_interrupted_beta_takeover(tmp)
            )
            real_atomic_write = config_overlay.atomic_write_text

            metadata_path = config_overlay.takeover_metadata_path(beta_backup_path)

            def fail_cleanup_journal_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
                if path == metadata_path:
                    raise OSError("simulated cleanup journal write failure")
                real_atomic_write(path, text, encoding=encoding)

            with patch("config_overlay.atomic_write_text", fail_cleanup_journal_write):
                with self.assertRaisesRegex(OSError, "cleanup journal write failure"):
                    restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertTrue(beta_backup_path.exists())
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(status, "interrupted_takeover_discarded")
            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertFalse(beta_backup_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_completed_takeover_sidecar_delete_failure_resumes_original_owner_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            stable_backup_path = tmp / "stable.backup.toml"
            beta_backup_path = tmp / "beta.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text(RUST_0_145_AGENTS_CONFIG, encoding="utf-8")

            apply_overlay(
                config_path,
                stable_backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
                owner="release",
            )
            stable_active = config_path.read_bytes()
            apply_overlay(
                config_path,
                beta_backup_path,
                catalog_path,
                "http://127.0.0.1:9109",
                owner="beta",
                takeover=True,
            )

            metadata_path = config_overlay.takeover_metadata_path(beta_backup_path)
            real_unlink = Path.unlink

            def fail_sidecar_delete(path: Path, *args: object, **kwargs: object) -> None:
                if path == metadata_path:
                    raise OSError("simulated sidecar delete failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_sidecar_delete):
                with self.assertRaisesRegex(OSError, "sidecar delete failure"):
                    restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertFalse(beta_backup_path.exists())
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, beta_backup_path, unified_history=True)

            self.assertEqual(status, "restored_takeover_backup")
            self.assertEqual(config_path.read_bytes(), stable_active)
            self.assertFalse(metadata_path.exists())

    def test_unowned_unified_takeover_journal_write_failure_preserves_recovery_chain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, original, active, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            metadata_before = metadata_path.read_bytes()
            real_atomic_write = config_overlay.atomic_write_text

            def fail_journal_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
                if path == metadata_path:
                    raise OSError("simulated unified restore journal failure")
                real_atomic_write(path, text, encoding=encoding)

            with patch("config_overlay.atomic_write_text", fail_journal_write):
                with self.assertRaisesRegex(OSError, "unified restore journal failure"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), active)
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertEqual(metadata_path.read_bytes(), metadata_before)

    def test_unowned_unified_takeover_resumes_after_ambiguous_journal_publish_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, original, active, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            real_atomic_write = config_overlay.atomic_write_text

            def publish_journal_then_fail(
                path: Path,
                text: str,
                *,
                encoding: str = "utf-8",
            ) -> None:
                real_atomic_write(path, text, encoding=encoding)
                if path == metadata_path and "cleanup_source_sha256" in text:
                    raise OSError("simulated ambiguous journal publication")

            with patch("config_overlay.atomic_write_text", publish_journal_then_fail):
                with self.assertRaisesRegex(OSError, "ambiguous journal publication"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), active)
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertIn(
                "cleanup_recovery_sha256",
                metadata_path.read_text(encoding="utf-8"),
            )

            status = restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(status, "injected")
            self.assertFalse(backup_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_unowned_unified_takeover_final_publish_failure_resumes_from_journal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, original, active, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            real_atomic_write = config_overlay.atomic_write_text

            def fail_final_publish(path: Path, text: str, *, encoding: str = "utf-8") -> None:
                if path == config_path:
                    raise OSError("simulated unified final publish failure")
                real_atomic_write(path, text, encoding=encoding)

            with patch("config_overlay.atomic_write_text", fail_final_publish):
                with self.assertRaisesRegex(OSError, "unified final publish failure"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), active)
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, backup_path, unified_history=True)
            restored = tomllib.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(status, "injected")
            self.assertEqual(restored["agents"], tomllib.loads(RUST_0_145_AGENTS_CONFIG)["agents"])
            self.assertEqual(restored["model_provider"], "custom")
            self.assertEqual(restored["model_providers"]["custom"]["name"], "OpenAI")
            self.assertFalse(backup_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_unowned_unified_takeover_resumes_after_ambiguous_final_publish_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, original, _, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            real_atomic_write = config_overlay.atomic_write_text
            published: bytes | None = None

            def publish_final_then_fail(
                path: Path,
                text: str,
                *,
                encoding: str = "utf-8",
            ) -> None:
                nonlocal published
                real_atomic_write(path, text, encoding=encoding)
                if path == config_path and "OpenAI" in text:
                    published = config_path.read_bytes()
                    raise OSError("simulated ambiguous final publication")

            with patch("config_overlay.atomic_write_text", publish_final_then_fail):
                with self.assertRaisesRegex(OSError, "ambiguous final publication"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            self.assertIsNotNone(published)
            self.assertNotEqual(published, original)
            self.assertEqual(config_path.read_bytes(), published)
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(status, "injected")
            self.assertEqual(config_path.read_bytes(), published)
            self.assertFalse(backup_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_unowned_unified_takeover_backup_delete_failure_resumes_published_final(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, original, _, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            real_unlink = Path.unlink

            def fail_backup_delete(path: Path, *args: object, **kwargs: object) -> None:
                if path == backup_path:
                    raise OSError("simulated unified backup delete failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_backup_delete):
                with self.assertRaisesRegex(OSError, "unified backup delete failure"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            published = config_path.read_bytes()
            self.assertNotEqual(published, original)
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(status, "injected")
            self.assertEqual(config_path.read_bytes(), published)
            self.assertFalse(backup_path.exists())
            self.assertFalse(metadata_path.exists())

    def test_unowned_unified_takeover_resumes_after_ambiguous_backup_delete_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, _, _, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            real_unlink = Path.unlink

            def delete_backup_then_fail(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> None:
                real_unlink(path, *args, **kwargs)
                if path == backup_path:
                    raise OSError("simulated ambiguous backup deletion")

            with patch.object(Path, "unlink", delete_backup_then_fail):
                with self.assertRaisesRegex(OSError, "ambiguous backup deletion"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            published = config_path.read_bytes()
            self.assertFalse(backup_path.exists())
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(status, "injected")
            self.assertEqual(config_path.read_bytes(), published)
            self.assertFalse(metadata_path.exists())

    def test_unowned_unified_takeover_sidecar_delete_failure_resumes_published_final(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, _, _, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            real_unlink = Path.unlink

            def fail_sidecar_delete(path: Path, *args: object, **kwargs: object) -> None:
                if path == metadata_path:
                    raise OSError("simulated unified sidecar delete failure")
                real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_sidecar_delete):
                with self.assertRaisesRegex(OSError, "unified sidecar delete failure"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            published = config_path.read_bytes()
            self.assertFalse(backup_path.exists())
            self.assertTrue(metadata_path.exists())

            status = restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(status, "injected")
            self.assertEqual(config_path.read_bytes(), published)
            self.assertFalse(metadata_path.exists())

    def test_unowned_unified_takeover_is_idempotent_after_ambiguous_sidecar_delete_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, _, _, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            real_unlink = Path.unlink

            def delete_sidecar_then_fail(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> None:
                real_unlink(path, *args, **kwargs)
                if path == metadata_path:
                    raise OSError("simulated ambiguous sidecar deletion")

            with patch.object(Path, "unlink", delete_sidecar_then_fail):
                with self.assertRaisesRegex(OSError, "ambiguous sidecar deletion"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            published = config_path.read_bytes()
            self.assertFalse(backup_path.exists())
            self.assertFalse(metadata_path.exists())

            status = restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(status, "already_unified")
            self.assertEqual(config_path.read_bytes(), published)

    def test_apply_and_context_guard_reject_invalid_takeover_metadata_without_mutation(self):
        for operation in ("apply", "context_guard"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                config_path, backup_path, original, active, metadata_path = (
                    self._unowned_with_active_beta_takeover(tmp)
                )
                invalid_metadata = b'{"version":2,"future_state":"unknown"}\n'
                metadata_path.write_bytes(invalid_metadata)
                state_path = tmp / "context-guard-state.json"
                catalog_path = self._official_budget_catalog(tmp)

                with self.assertRaisesRegex(RuntimeError, "takeover metadata is invalid"):
                    if operation == "apply":
                        apply_overlay(
                            config_path,
                            backup_path,
                            catalog_path,
                            "http://127.0.0.1:9109",
                            owner="beta",
                            takeover=True,
                        )
                    else:
                        set_context_guard(
                            config_path,
                            backup_path,
                            state_path,
                            enabled=True,
                            catalog_path=catalog_path,
                        )

                self.assertEqual(config_path.read_bytes(), active)
                self.assertEqual(backup_path.read_bytes(), original)
                self.assertEqual(metadata_path.read_bytes(), invalid_metadata)
                self.assertFalse(state_path.exists())

    def test_new_managed_write_resumes_pending_cleanup_before_publication(self):
        for operation in ("apply", "context_guard"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                config_path, backup_path, metadata_path = (
                    self._unowned_with_pending_unified_cleanup(tmp)
                )
                catalog_path = self._official_budget_catalog(tmp)
                state_path = tmp / "context-guard-state.json"

                if operation == "apply":
                    apply_overlay(
                        config_path,
                        backup_path,
                        catalog_path,
                        "http://127.0.0.1:9099",
                        owner="release",
                    )
                    self.assertEqual(
                        config_overlay.overlay_owner(
                            config_path.read_text(encoding="utf-8")
                        ),
                        "release",
                    )
                    self.assertTrue(backup_path.exists())
                else:
                    result = set_context_guard(
                        config_path,
                        backup_path,
                        state_path,
                        enabled=True,
                        catalog_path=catalog_path,
                    )
                    self.assertFalse(result["enabled"])
                    self.assertFalse(backup_path.exists())

                self.assertFalse(metadata_path.exists())

    def test_context_guard_rejects_interrupted_takeover_without_touching_recovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, catalog_path, active = (
                self._stable_with_interrupted_beta_takeover(tmp)
            )
            metadata_path = config_overlay.takeover_metadata_path(backup_path)
            metadata_before = metadata_path.read_bytes()
            state_path = tmp / "context-guard-state.json"

            with self.assertRaisesRegex(RuntimeError, "interrupted takeover recovery is pending"):
                set_context_guard(
                    config_path,
                    backup_path,
                    state_path,
                    enabled=True,
                    catalog_path=catalog_path,
                )

            self.assertEqual(config_path.read_bytes(), active)
            self.assertEqual(backup_path.read_bytes(), active)
            self.assertEqual(metadata_path.read_bytes(), metadata_before)
            self.assertFalse(state_path.exists())

    def test_existing_malformed_or_future_takeover_metadata_fails_closed(self):
        invalid_payloads = {
            "truncated": "{",
            "non_object": "[]",
            "future_version": json.dumps(
                {
                    "version": 2,
                    "takeover_owner": "beta",
                    "original_owner": None,
                }
            ),
            "boolean_version": json.dumps(
                {
                    "version": True,
                    "takeover_owner": "beta",
                    "original_owner": None,
                }
            ),
            "duplicate_version": (
                '{"version":2,"version":1,"takeover_owner":"beta",'
                '"original_owner":null}'
            ),
            "duplicate_owner": (
                '{"version":1,"takeover_owner":"release",'
                '"takeover_owner":"beta","original_owner":null}'
            ),
            "same_owner": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": "beta",
                    "original_owner": "beta",
                }
            ),
            "unknown_owner": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": "future-owner",
                    "original_owner": None,
                }
            ),
            "non_scalar_owner": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": [],
                    "original_owner": None,
                }
            ),
            "unknown_field": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": "beta",
                    "original_owner": None,
                    "future_field": True,
                }
            ),
            "partial_journal": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": "beta",
                    "original_owner": None,
                    "cleanup_source_sha256": "0" * 64,
                }
            ),
            "null_journal": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": "beta",
                    "original_owner": None,
                    "cleanup_source_sha256": None,
                    "cleanup_recovery_sha256": None,
                    "cleanup_final_sha256": None,
                    "cleanup_status": None,
                }
            ),
            "unknown_status": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": "beta",
                    "original_owner": None,
                    "cleanup_source_sha256": "0" * 64,
                    "cleanup_recovery_sha256": "1" * 64,
                    "cleanup_final_sha256": "2" * 64,
                    "cleanup_status": "future_status",
                }
            ),
            "malformed_digest": json.dumps(
                {
                    "version": 1,
                    "takeover_owner": "beta",
                    "original_owner": None,
                    "cleanup_source_sha256": "not-a-digest",
                    "cleanup_recovery_sha256": "1" * 64,
                    "cleanup_final_sha256": "2" * 64,
                    "cleanup_status": "injected",
                }
            ),
        }

        for name, payload in invalid_payloads.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                config_path, backup_path, original, active, metadata_path = (
                    self._unowned_with_active_beta_takeover(tmp)
                )
                metadata_path.write_text(payload, encoding="utf-8")

                with self.assertRaisesRegex(RuntimeError, "takeover metadata is invalid"):
                    restore_overlay(config_path, backup_path, unified_history=True)

                self.assertEqual(config_path.read_bytes(), active)
                self.assertEqual(backup_path.read_bytes(), original)
                self.assertEqual(metadata_path.read_text(encoding="utf-8"), payload)

    def test_existing_unreadable_takeover_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, original, active, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            metadata_before = metadata_path.read_bytes()
            real_read_text = Path.read_text

            def fail_sidecar_read(path: Path, *args: object, **kwargs: object) -> str:
                if path == metadata_path:
                    raise OSError("simulated transient sidecar read failure")
                return real_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", fail_sidecar_read):
                with self.assertRaisesRegex(RuntimeError, "takeover metadata is invalid"):
                    restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), active)
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertEqual(metadata_path.read_bytes(), metadata_before)

    def test_valid_takeover_metadata_with_unrecognized_live_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path, backup_path, original, active, metadata_path = (
                self._unowned_with_active_beta_takeover(tmp)
            )
            unrecognized = active.replace(b"# owner = beta", b"# owner = release")
            config_path.write_bytes(unrecognized)
            metadata_before = metadata_path.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "state is not recognized"):
                restore_overlay(config_path, backup_path, unified_history=True)

            self.assertEqual(config_path.read_bytes(), unrecognized)
            self.assertEqual(backup_path.read_bytes(), original)
            self.assertEqual(metadata_path.read_bytes(), metadata_before)

    def test_pending_unified_cleanup_rejects_inconsistent_artifacts_and_journal(self):
        cases = (
            ("live_artifact", "live config is missing or diverged"),
            ("recovery_artifact", "recovery backup is missing or diverged"),
            ("source_digest", "live config is missing or diverged"),
            ("recovery_digest", "recovery backup is missing or diverged"),
            ("final_digest", "intended final config is inconsistent"),
            ("terminal_status", "terminal status is inconsistent"),
            ("takeover_owner", "live config is missing or diverged"),
            ("original_owner", "recovery backup is missing or diverged"),
        )

        for case, expected_error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                config_path, backup_path, metadata_path = (
                    self._unowned_with_pending_unified_cleanup(Path(tmpdir))
                )
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

                if case == "live_artifact":
                    config_path.write_text(
                        config_path.read_text(encoding="utf-8") + "# divergent live edit\n",
                        encoding="utf-8",
                    )
                elif case == "recovery_artifact":
                    backup_path.write_text(
                        backup_path.read_text(encoding="utf-8")
                        + "# divergent recovery edit\n",
                        encoding="utf-8",
                    )
                elif case == "source_digest":
                    metadata["cleanup_source_sha256"] = "d" * 64
                elif case == "recovery_digest":
                    metadata["cleanup_recovery_sha256"] = "e" * 64
                elif case == "final_digest":
                    metadata["cleanup_final_sha256"] = "f" * 64
                elif case == "terminal_status":
                    metadata["cleanup_status"] = "already_unified"
                elif case == "takeover_owner":
                    metadata["takeover_owner"] = "release"
                else:
                    metadata["original_owner"] = "release"

                if case not in {"live_artifact", "recovery_artifact"}:
                    metadata_path.write_text(
                        json.dumps(metadata, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                config_before = config_path.read_bytes()
                backup_before = backup_path.read_bytes()
                metadata_before = metadata_path.read_bytes()

                with self.assertRaisesRegex(RuntimeError, expected_error):
                    restore_overlay(config_path, backup_path, unified_history=True)

                self.assertEqual(config_path.read_bytes(), config_before)
                self.assertEqual(backup_path.read_bytes(), backup_before)
                self.assertEqual(metadata_path.read_bytes(), metadata_before)

    def test_agents_config_survives_connect_restart_readback_and_restore_for_supported_shapes(self):
        legacy_enabled = RUST_0_145_AGENTS_CONFIG.replace(
            "max_concurrent_threads_per_session = 7",
            "max_threads = 7",
        ).replace(
            "multi_agent_v2 = { enabled = false, max_concurrent_threads_per_session = 5, tool_namespace = \"team_tools\" }",
            "multi_agent_v2 = { enabled = true, max_concurrent_threads_per_session = 9, tool_namespace = \"team_tools\" }",
        )
        no_backend_or_defaults = RUST_0_145_AGENTS_CONFIG.replace(
            "max_concurrent_threads_per_session = 7",
            "max_threads = 3",
        ).replace(
            'default_subagent_model = "gpt-5.6-terra"\n',
            "",
        ).replace(
            'default_subagent_reasoning_effort = "high"\n',
            "",
        ).replace(
            'multi_agent_v2 = { enabled = false, max_concurrent_threads_per_session = 5, tool_namespace = "team_tools" }\n',
            "",
        )
        cases = (
            ("canonical_structured_disabled", RUST_0_145_AGENTS_CONFIG, False),
            ("legacy_alias_structured_enabled", legacy_enabled, True),
            ("legacy_alias_without_backend_or_defaults", no_backend_or_defaults, None),
        )

        for name, original, expected_v2_enabled in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                config_path = tmp / "config.toml"
                backup_path = tmp / "config.backup.toml"
                catalog_path = tmp / "catalog.json"
                original_bytes = original.encode()
                expected = tomllib.loads(original)
                config_path.write_bytes(original_bytes)

                apply_overlay(
                    config_path,
                    backup_path,
                    catalog_path,
                    "http://127.0.0.1:9099",
                )
                apply_overlay(
                    config_path,
                    backup_path,
                    catalog_path,
                    "http://127.0.0.1:9099",
                )

                readback = tomllib.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(readback["agents"], expected["agents"])
                self.assertEqual(backup_path.read_bytes(), original_bytes)
                if expected_v2_enabled is None:
                    self.assertNotIn("multi_agent_v2", readback["features"])
                    self.assertNotIn("default_subagent_model", readback["agents"])
                    self.assertNotIn("default_subagent_reasoning_effort", readback["agents"])
                else:
                    self.assertEqual(
                        readback["features"]["multi_agent_v2"],
                        expected["features"]["multi_agent_v2"],
                    )
                    self.assertIs(
                        readback["features"]["multi_agent_v2"]["enabled"],
                        expected_v2_enabled,
                    )

                restore_overlay(config_path, backup_path)
                self.assertEqual(config_path.read_bytes(), original_bytes)

    def test_explicit_multi_agent_v2_table_survives_connect_restart_and_restore(self):
        original = RUST_0_145_AGENTS_CONFIG.replace(
            'multi_agent_v2 = { enabled = false, max_concurrent_threads_per_session = 5, tool_namespace = "team_tools" }\n',
            "",
        ) + (
            "\n[features.multi_agent_v2]\n"
            "enabled = false\n"
            "max_concurrent_threads_per_session = 5\n"
            'tool_namespace = "team_tools"\n'
        )
        expected = tomllib.loads(original)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            original_bytes = original.encode()
            config_path.write_bytes(original_bytes)

            apply_overlay(
                config_path,
                backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
            )
            apply_overlay(
                config_path,
                backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
            )

            readback = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(readback["agents"], expected["agents"])
            self.assertEqual(
                readback["features"]["multi_agent_v2"],
                expected["features"]["multi_agent_v2"],
            )

            restore_overlay(config_path, backup_path)
            self.assertEqual(config_path.read_bytes(), original_bytes)

    def test_successful_stable_beta_takeover_preserves_one_agents_tree_and_each_owner_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            stable_backup_path = tmp / "stable.backup.toml"
            beta_backup_path = tmp / "beta.backup.toml"
            catalog_path = tmp / "catalog.json"
            original = RUST_0_145_AGENTS_CONFIG.encode()
            expected = tomllib.loads(RUST_0_145_AGENTS_CONFIG)
            config_path.write_bytes(original)

            apply_overlay(
                config_path,
                stable_backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
                owner="release",
            )
            stable_active = config_path.read_bytes()

            apply_overlay(
                config_path,
                beta_backup_path,
                catalog_path,
                "http://127.0.0.1:9109",
                owner="beta",
                takeover=True,
            )
            apply_overlay(
                config_path,
                beta_backup_path,
                catalog_path,
                "http://127.0.0.1:9109",
                owner="beta",
            )

            beta_text = config_path.read_text(encoding="utf-8")
            beta_readback = tomllib.loads(beta_text)
            self.assertEqual(beta_readback["agents"], expected["agents"])
            self.assertEqual(
                beta_readback["features"]["multi_agent_v2"],
                expected["features"]["multi_agent_v2"],
            )
            self.assertEqual(beta_text.count("[agents]"), 1)
            self.assertEqual(beta_text.count("[agents.researcher]"), 1)
            self.assertEqual(beta_text.count("[agents.reviewer]"), 1)

            status = restore_overlay(config_path, beta_backup_path, unified_history=True)
            self.assertEqual(status, "restored_takeover_backup")
            self.assertEqual(config_path.read_bytes(), stable_active)

            restore_overlay(config_path, stable_backup_path)
            self.assertEqual(config_path.read_bytes(), original)

    def test_successful_beta_release_takeover_preserves_agents_and_each_owner_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            beta_backup_path = tmp / "beta.backup.toml"
            release_backup_path = tmp / "release.backup.toml"
            catalog_path = tmp / "catalog.json"
            original = RUST_0_145_AGENTS_CONFIG.encode()
            expected = tomllib.loads(RUST_0_145_AGENTS_CONFIG)
            config_path.write_bytes(original)

            apply_overlay(
                config_path,
                beta_backup_path,
                catalog_path,
                "http://127.0.0.1:9109",
                owner="beta",
            )
            beta_active = config_path.read_bytes()

            apply_overlay(
                config_path,
                release_backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
                owner="release",
                takeover=True,
            )
            apply_overlay(
                config_path,
                release_backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
                owner="release",
            )

            release_readback = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(release_readback["agents"], expected["agents"])
            self.assertEqual(
                release_readback["features"]["multi_agent_v2"],
                expected["features"]["multi_agent_v2"],
            )

            status = restore_overlay(config_path, release_backup_path, unified_history=True)
            self.assertEqual(status, "restored_takeover_backup")
            self.assertEqual(config_path.read_bytes(), beta_active)

            restore_overlay(config_path, beta_backup_path)
            self.assertEqual(config_path.read_bytes(), original)

    def test_connect_without_agents_or_multi_agent_v2_does_not_invent_user_choices(self):
        original = '[features]\nhooks = true\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            config_path = tmp / "config.toml"
            backup_path = tmp / "config.backup.toml"
            catalog_path = tmp / "catalog.json"
            config_path.write_text(original, encoding="utf-8")

            apply_overlay(
                config_path,
                backup_path,
                catalog_path,
                "http://127.0.0.1:9099",
            )

            readback = tomllib.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("agents", readback)
            self.assertNotIn("multi_agent_v2", readback["features"])

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

    def test_restore_rejects_preexisting_same_owner_takeover_sidecar(self):
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
            config_before = config.read_bytes()
            backup_before = backup.read_bytes()
            metadata_before = metadata.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "takeover metadata is invalid"):
                restore_overlay(config, backup, unified_history=True)

            self.assertEqual(config.read_bytes(), config_before)
            self.assertEqual(backup.read_bytes(), backup_before)
            self.assertEqual(metadata.read_bytes(), metadata_before)

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
