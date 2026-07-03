from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from history_overlay import (
    apply_history_overlay,
    history_model_map_path,
    normalize_history_provider_fast,
    promote_custom_history_to_openai,
    restore_history_overlay,
)


class HistoryOverlayTests(unittest.TestCase):
    def test_apply_and_restore_state_and_jsonl_provider_bucket(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_dir = root / ".codex"
            sessions_dir = codex_dir / "sessions" / "2026" / "06" / "28"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-test.jsonl"
            session_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": "thread-openai",
                                    "model_provider": "openai",
                                },
                            },
                            separators=(",", ":"),
                        ),
                        json.dumps({"type": "event_msg", "payload": {"message": "keep me"}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            db_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT)")
            connection.execute("INSERT INTO threads VALUES ('thread-openai', 'openai')")
            connection.execute("INSERT INTO threads VALUES ('thread-custom', 'custom')")
            connection.commit()
            connection.close()

            backup_root = root / "backup"
            ledger_path = backup_root / "ledger.json"
            ledger = apply_history_overlay(codex_dir, backup_root, ledger_path)

            self.assertEqual(len(ledger["state"][0]["thread_ids"]), 1)
            self.assertEqual(len(ledger["jsonl"]), 1)
            connection = sqlite3.connect(db_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-openai"], "custom")
            self.assertEqual(providers["thread-custom"], "custom")
            first_line = session_file.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(json.loads(first_line)["payload"]["model_provider"], "custom")
            self.assertTrue((backup_root / "jsonl" / "sessions" / "2026" / "06" / "28" / "rollout-test.jsonl").exists())

            restored = restore_history_overlay(ledger_path)

            self.assertEqual(restored["state_rows"], 1)
            self.assertEqual(restored["jsonl_files"], 1)
            connection = sqlite3.connect(db_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-openai"], "openai")
            self.assertEqual(providers["thread-custom"], "custom")
            first_line = session_file.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(json.loads(first_line)["payload"]["model_provider"], "openai")

    def test_restore_migrates_new_custom_sessions_and_sanitizes_bad_reasoning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_dir = root / ".codex"
            sessions_dir = codex_dir / "sessions" / "2026" / "06" / "28"
            sessions_dir.mkdir(parents=True)

            openai_session_file = sessions_dir / "rollout-openai.jsonl"
            openai_session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-openai", "model_provider": "openai"}})
                + "\n",
                encoding="utf-8",
            )
            existing_custom_file = sessions_dir / "rollout-existing-custom.jsonl"
            existing_custom_file.write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"id": "thread-existing-custom", "model_provider": "custom"}}
                )
                + "\n",
                encoding="utf-8",
            )

            db_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT)")
            connection.execute("INSERT INTO threads VALUES ('thread-openai', 'openai')")
            connection.execute("INSERT INTO threads VALUES ('thread-existing-custom', 'custom')")
            connection.commit()
            connection.close()

            backup_root = root / "backup"
            ledger_path = backup_root / "ledger.json"
            apply_history_overlay(codex_dir, backup_root, ledger_path)

            bad_reasoning = {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "third party summary"}],
                    "encrypted_content": "The user just typed test.",
                },
            }
            with openai_session_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(bad_reasoning) + "\n")

            new_custom_file = sessions_dir / "rollout-new-custom.jsonl"
            new_custom_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"type": "session_meta", "payload": {"id": "thread-new-custom", "model_provider": "custom"}}
                        ),
                        json.dumps(bad_reasoning),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(db_path)
            connection.execute("INSERT INTO threads VALUES ('thread-new-custom', 'custom')")
            connection.commit()
            connection.close()

            restored = restore_history_overlay(ledger_path)

            self.assertEqual(restored["new_state_rows"], 1)
            self.assertEqual(restored["new_jsonl_files"], 1)
            connection = sqlite3.connect(db_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-openai"], "openai")
            self.assertEqual(providers["thread-new-custom"], "openai")
            self.assertEqual(providers["thread-existing-custom"], "custom")

            openai_lines = [json.loads(line) for line in openai_session_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(openai_lines[0]["payload"]["model_provider"], "openai")
            self.assertNotIn("encrypted_content", openai_lines[1]["payload"])

            new_custom_lines = [json.loads(line) for line in new_custom_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(new_custom_lines[0]["payload"]["model_provider"], "openai")
            self.assertNotIn("encrypted_content", new_custom_lines[1]["payload"])

            existing_custom_first_line = existing_custom_file.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(json.loads(existing_custom_first_line)["payload"]["model_provider"], "custom")

    def test_promote_custom_history_to_openai_converts_all_custom_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            custom_file = sessions_dir / "rollout-custom.jsonl"
            custom_file.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "thread-custom", "model_provider": "custom"}}),
                        json.dumps({"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "not-official"}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT)")
                connection.execute("INSERT INTO threads VALUES ('thread-custom', 'custom')")
                connection.commit()
            finally:
                connection.close()

            result = promote_custom_history_to_openai(codex_dir, codex_dir / "backup")

            self.assertEqual(result["state_rows"], 1)
            self.assertEqual(result["jsonl_files"], 1)
            connection = sqlite3.connect(state_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-custom"], "openai")
            lines = [json.loads(line) for line in custom_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["payload"]["model_provider"], "openai")
            self.assertNotIn("encrypted_content", lines[1]["payload"])

    def test_promote_custom_history_to_openai_uses_official_fallback_for_third_party_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, model TEXT)")
                connection.execute("INSERT INTO threads VALUES ('thread-custom', 'custom', 'kimi-k2.7-code')")
                connection.commit()
            finally:
                connection.close()

            result = promote_custom_history_to_openai(codex_dir, codex_dir / "backup")

            self.assertEqual(result["state_rows"], 1)
            self.assertEqual(result["state_model_rows"], 1)
            connection = sqlite3.connect(state_path)
            try:
                row = connection.execute("SELECT model_provider, model FROM threads WHERE id = 'thread-custom'").fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("openai", "gpt-5.5"))
            model_map = json.loads(history_model_map_path(codex_dir).read_text(encoding="utf-8"))
            self.assertEqual(model_map["threads"]["thread-custom"]["model"], "kimi-k2.7-code")

    def test_normalize_history_provider_fast_only_rewrites_session_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            custom_file = sessions_dir / "rollout-custom.jsonl"
            reasoning = {
                "type": "response_item",
                "payload": {"type": "reasoning", "encrypted_content": "not-official"},
            }
            custom_file.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "thread-custom", "model_provider": "custom"}}),
                        json.dumps(reasoning),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT)")
                connection.execute("INSERT INTO threads VALUES ('thread-custom', 'custom')")
                connection.commit()
            finally:
                connection.close()

            result = normalize_history_provider_fast(codex_dir, codex_dir / "backup", "openai")

            self.assertEqual(result["state_rows"], 1)
            self.assertEqual(len(result["jsonl"]), 1)
            self.assertIn("old_first_line", result["jsonl"][0])
            self.assertFalse((codex_dir / "backup" / "jsonl").exists())
            connection = sqlite3.connect(state_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-custom"], "openai")
            lines = [json.loads(line) for line in custom_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["payload"]["model_provider"], "openai")
            self.assertEqual(lines[1]["payload"]["encrypted_content"], "not-official")

    def test_normalize_history_provider_fast_scans_filesystem_beside_state_rollout_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)

            indexed_file = sessions_dir / "rollout-indexed.jsonl"
            indexed_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-indexed", "model_provider": "custom"}})
                + "\n",
                encoding="utf-8",
            )
            orphan_file = sessions_dir / "rollout-orphan.jsonl"
            orphan_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-orphan", "model_provider": "openai"}})
                + "\n",
                encoding="utf-8",
            )

            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, rollout_path TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-indexed', 'openai', ?)",
                    (str(indexed_file),),
                )
                connection.commit()
            finally:
                connection.close()

            result = normalize_history_provider_fast(codex_dir, codex_dir / "backup", "custom")

            self.assertEqual(result["plan_source"], "state_rollout_path+filesystem_scan")
            self.assertEqual(result["planned_session_files"], 2)
            self.assertEqual(result["state_rows"], 1)
            self.assertEqual(len(result["jsonl"]), 1)
            connection = sqlite3.connect(state_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-indexed"], "custom")
            indexed_first_line = indexed_file.read_text(encoding="utf-8").splitlines()[0]
            orphan_first_line = orphan_file.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(json.loads(indexed_first_line)["payload"]["model_provider"], "custom")
            self.assertEqual(json.loads(orphan_first_line)["payload"]["model_provider"], "custom")

    def test_normalize_history_provider_fast_preserves_third_party_models_across_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, model TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-custom-openai', 'custom', 'openai/gpt-5.5')"
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-openai-prefixed', 'openai', 'openai/gpt-5.4')"
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-custom-kimi', 'custom', 'kimi-k2.7-code')"
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-custom-volc', 'custom', 'volc/glm-5.2')"
                )
                connection.commit()
            finally:
                connection.close()

            result = normalize_history_provider_fast(codex_dir, codex_dir / "backup-official", "openai")

            self.assertEqual(result["state_rows"], 3)
            self.assertEqual(result["state_model_rows"], 4)
            connection = sqlite3.connect(state_path)
            try:
                rows = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute("SELECT id, model_provider, model FROM threads")
                }
            finally:
                connection.close()
            self.assertEqual(rows["thread-custom-openai"], ("openai", "gpt-5.5"))
            self.assertEqual(rows["thread-openai-prefixed"], ("openai", "gpt-5.4"))
            self.assertEqual(rows["thread-custom-kimi"], ("openai", "gpt-5.5"))
            self.assertEqual(rows["thread-custom-volc"], ("openai", "gpt-5.5"))
            model_map = json.loads(history_model_map_path(codex_dir).read_text(encoding="utf-8"))
            self.assertEqual(model_map["threads"]["thread-custom-kimi"]["model"], "kimi-k2.7-code")
            self.assertEqual(model_map["threads"]["thread-custom-volc"]["model"], "volc/glm-5.2")

            result = normalize_history_provider_fast(codex_dir, codex_dir / "backup-custom", "custom")

            self.assertEqual(result["state_rows"], 4)
            self.assertEqual(result["state_model_rows"], 4)
            connection = sqlite3.connect(state_path)
            try:
                rows = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute("SELECT id, model_provider, model FROM threads")
                }
            finally:
                connection.close()
            self.assertEqual(rows["thread-custom-openai"], ("custom", "openai/gpt-5.5"))
            self.assertEqual(rows["thread-openai-prefixed"], ("custom", "openai/gpt-5.4"))
            self.assertEqual(rows["thread-custom-kimi"], ("custom", "kimi-k2.7-code"))
            self.assertEqual(rows["thread-custom-volc"], ("custom", "volc/glm-5.2"))
            model_map = json.loads(history_model_map_path(codex_dir).read_text(encoding="utf-8"))
            self.assertEqual(model_map["threads"], {})

    def test_normalize_history_provider_fast_rolls_back_model_map_when_jsonl_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-custom.jsonl"
            session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-custom", "model_provider": "custom"}})
                + "\n",
                encoding="utf-8",
            )
            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, model TEXT, rollout_path TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-custom', 'custom', 'kimi-k2.7-code', ?)",
                    (str(session_file),),
                )
                connection.commit()
            finally:
                connection.close()

            with patch("history_overlay.apply_session_file_provider_fast", side_effect=RuntimeError("jsonl failed")):
                with self.assertRaises(RuntimeError):
                    normalize_history_provider_fast(codex_dir, codex_dir / "backup", "openai")

            connection = sqlite3.connect(state_path)
            try:
                row = connection.execute("SELECT model_provider, model FROM threads WHERE id = 'thread-custom'").fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("custom", "kimi-k2.7-code"))
            map_path = history_model_map_path(codex_dir)
            if map_path.exists():
                self.assertEqual(json.loads(map_path.read_text(encoding="utf-8"))["threads"], {})

    def test_normalize_history_provider_fast_rolls_back_multiple_sqlite_homes_independently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_dir = root / ".codex"
            external_sqlite_home = root / "sqlite-home"
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            external_sqlite_home.mkdir(parents=True)
            (codex_dir / "config.toml").write_text(
                f"sqlite_home = \"{external_sqlite_home.as_posix()}\"\n",
                encoding="utf-8",
            )
            session_file = sessions_dir / "rollout-custom.jsonl"
            session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-jsonl", "model_provider": "custom"}})
                + "\n",
                encoding="utf-8",
            )

            main_state = codex_dir / "state_5.sqlite"
            external_state = external_sqlite_home / "state_5.sqlite"
            for path, thread_id, model in (
                (main_state, "thread-main", "kimi-k2.7-code"),
                (external_state, "thread-external", "volc/glm-5.2"),
            ):
                connection = sqlite3.connect(path)
                try:
                    connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, model TEXT)")
                    connection.execute("INSERT INTO threads VALUES (?, 'custom', ?)", (thread_id, model))
                    connection.commit()
                finally:
                    connection.close()

            with patch("history_overlay.apply_session_file_provider_fast", side_effect=RuntimeError("jsonl failed")):
                with self.assertRaises(RuntimeError):
                    normalize_history_provider_fast(codex_dir, codex_dir / "backup", "openai")

            for path, thread_id, model in (
                (main_state, "thread-main", "kimi-k2.7-code"),
                (external_state, "thread-external", "volc/glm-5.2"),
            ):
                connection = sqlite3.connect(path)
                try:
                    row = connection.execute("SELECT id, model_provider, model FROM threads").fetchone()
                finally:
                    connection.close()
                self.assertEqual(row, (thread_id, "custom", model))


if __name__ == "__main__":
    unittest.main()
