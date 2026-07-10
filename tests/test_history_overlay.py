from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from history_overlay import (
    apply_history_overlay,
    apply_session_file_provider_fast,
    ensure_unified_history_bucket,
    inspect_unified_history_bucket,
    main as history_overlay_main,
    migrate_official_history_to_unified,
    normalize_history_provider_fast,
    plan_session_file_provider_fast,
    promote_custom_history_to_openai,
    repair_history_bucket,
    restore_history_overlay,
    restore_official_history_from_unified,
    restore_repair_backups,
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
            backup_root.mkdir(parents=True)
            ledger_lock_path = ledger_path.with_name("ledger.json.lock")
            ledger_lock_path.write_text("pid=0\nacquired_at_millis=0\n", encoding="utf-8")
            ledger = apply_history_overlay(codex_dir, backup_root, ledger_path)

            self.assertEqual(len(ledger["state"][0]["thread_ids"]), 1)
            self.assertEqual(len(ledger["jsonl"]), 1)
            self.assertFalse(ledger_lock_path.exists())
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

    def test_restore_uses_ledger_only_and_does_not_rewrite_session_content(self):
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

            self.assertEqual(restored["new_state_rows"], 0)
            self.assertEqual(restored["new_jsonl_files"], 0)
            connection = sqlite3.connect(db_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-openai"], "openai")
            self.assertEqual(providers["thread-new-custom"], "custom")
            self.assertEqual(providers["thread-existing-custom"], "custom")

            openai_lines = [json.loads(line) for line in openai_session_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(openai_lines[0]["payload"]["model_provider"], "openai")
            self.assertIn("encrypted_content", openai_lines[1]["payload"])

            new_custom_lines = [json.loads(line) for line in new_custom_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(new_custom_lines[0]["payload"]["model_provider"], "custom")
            self.assertIn("encrypted_content", new_custom_lines[1]["payload"])

            existing_custom_first_line = existing_custom_file.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(json.loads(existing_custom_first_line)["payload"]["model_provider"], "custom")

    def test_promote_custom_history_to_openai_without_ledger_is_safe_noop(self):
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

            self.assertEqual(result["status"], "no-ledger")
            self.assertEqual(result["state_rows"], 0)
            self.assertEqual(result["jsonl_restored"], 0)
            connection = sqlite3.connect(state_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-custom"], "custom")
            lines = [json.loads(line) for line in custom_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["payload"]["model_provider"], "custom")
            self.assertIn("encrypted_content", lines[1]["payload"])

    def test_promote_custom_history_to_openai_preserves_model_names(self):
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

            self.assertEqual(result["status"], "no-ledger")
            self.assertEqual(result["state_rows"], 0)
            self.assertEqual(result["state_model_rows"], 0)
            connection = sqlite3.connect(state_path)
            try:
                row = connection.execute("SELECT model_provider, model FROM threads WHERE id = 'thread-custom'").fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("custom", "kimi-k2.7-code"))

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

    def test_ensure_unified_history_bucket_repairs_dirty_rollout_paths_without_rewriting_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-openai.jsonl"
            session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-openai", "model_provider": "openai"}})
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
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, model TEXT, rollout_path TEXT)"
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-openai', 'openai', 'gpt-5.5', ?)",
                    (str(session_file),),
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-custom', 'custom', 'kimi-k2.7-code', '')"
                )
                connection.commit()
            finally:
                connection.close()

            result = ensure_unified_history_bucket(codex_dir, codex_dir / "backup")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["dirty_state_rows"], 1)
            self.assertEqual(result["state_rows"], 1)
            self.assertEqual(result["state_model_rows"], 0)
            self.assertEqual(result["jsonl_planned"], 1)
            self.assertEqual(result["jsonl_applied"], 1)
            self.assertEqual(result["jsonl_skipped"], 0)
            self.assertEqual(result["plan_source"], "dirty_rollout_path")
            connection = sqlite3.connect(state_path)
            try:
                rows = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute("SELECT id, model_provider, model FROM threads")
                }
            finally:
                connection.close()
            self.assertEqual(rows["thread-openai"], ("custom", "gpt-5.5"))
            self.assertEqual(rows["thread-custom"], ("custom", "kimi-k2.7-code"))
            indexed_first_line = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
            orphan_first_line = json.loads(orphan_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(indexed_first_line["payload"]["model_provider"], "custom")
            self.assertEqual(orphan_first_line["payload"]["model_provider"], "openai")

    def test_ensure_unified_history_bucket_returns_fast_when_no_openai_rows_exist(self):
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
                    "INSERT INTO threads VALUES ('thread-custom', 'custom', 'gpt-5.5', ?)",
                    (str(session_file),),
                )
                connection.commit()
            finally:
                connection.close()

            result = ensure_unified_history_bucket(codex_dir, codex_dir / "backup")

            self.assertEqual(result["status"], "already-unified")
            self.assertEqual(result["dirty_state_rows"], 0)
            self.assertEqual(result["jsonl_planned"], 0)
            self.assertFalse((codex_dir / "backup").exists())

    def test_inspect_unified_history_bucket_is_read_only_and_reports_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-openai.jsonl"
            session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-openai", "model_provider": "openai"}})
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
                    "INSERT INTO threads VALUES ('thread-openai', 'openai', 'gpt-5.6-sol', ?)",
                    (str(session_file),),
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-custom', 'custom', 'glm-5.2', '')"
                )
                connection.commit()
            finally:
                connection.close()

            before_db = state_path.read_bytes()
            before_jsonl = session_file.read_bytes()
            result = inspect_unified_history_bucket(codex_dir)

            self.assertEqual(result["status"], "needs_repair")
            self.assertEqual(result["dirty_state_rows"], 1)
            self.assertEqual(result["dirty_state_files"], 1)
            self.assertEqual(result["dirty_jsonl_files"], 1)
            self.assertEqual(state_path.read_bytes(), before_db)
            self.assertEqual(session_file.read_bytes(), before_jsonl)
            self.assertFalse((codex_dir / "backup").exists())

    def test_inspect_unified_history_bucket_reports_clean_custom_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT)")
                connection.execute("INSERT INTO threads VALUES ('thread-custom', 'custom')")
                connection.commit()
            finally:
                connection.close()

            result = inspect_unified_history_bucket(codex_dir)

            self.assertEqual(result["status"], "clean")
            self.assertEqual(result["dirty_state_rows"], 0)
            self.assertEqual(result["dirty_state_files"], 0)
            self.assertEqual(result["dirty_jsonl_files"], 0)

    def test_inspect_unified_history_cli_emits_json_without_creating_backups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = history_overlay_main(["inspect-unified", "--codex-dir", str(codex_dir)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "clean")
            self.assertEqual(list(codex_dir.iterdir()), [])

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

    def test_normalize_history_provider_fast_preserves_model_names_across_roundtrip(self):
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
            self.assertEqual(result["state_model_rows"], 0)
            connection = sqlite3.connect(state_path)
            try:
                rows = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute("SELECT id, model_provider, model FROM threads")
                }
            finally:
                connection.close()
            self.assertEqual(rows["thread-custom-openai"], ("openai", "openai/gpt-5.5"))
            self.assertEqual(rows["thread-openai-prefixed"], ("openai", "openai/gpt-5.4"))
            self.assertEqual(rows["thread-custom-kimi"], ("openai", "kimi-k2.7-code"))
            self.assertEqual(rows["thread-custom-volc"], ("openai", "volc/glm-5.2"))

            result = normalize_history_provider_fast(codex_dir, codex_dir / "backup-custom", "custom")

            self.assertEqual(result["state_rows"], 4)
            self.assertEqual(result["state_model_rows"], 0)
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

    def test_normalize_history_provider_fast_rolls_back_state_when_jsonl_write_fails(self):
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

    def test_ledger_scoped_repair_rolls_back_state_when_jsonl_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-openai.jsonl"
            session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-openai", "model_provider": "openai"}})
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
                    "INSERT INTO threads VALUES ('thread-openai', 'openai', ?)",
                    (str(session_file),),
                )
                connection.commit()
            finally:
                connection.close()

            backup_root = codex_dir / "backup"
            with patch("history_overlay.apply_session_file_provider_fast", side_effect=RuntimeError("jsonl failed")):
                with self.assertRaisesRegex(RuntimeError, "jsonl failed"):
                    repair_history_bucket(codex_dir, backup_root, "custom")

            connection = sqlite3.connect(state_path)
            try:
                provider = connection.execute(
                    "SELECT model_provider FROM threads WHERE id = 'thread-openai'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "openai")
            self.assertEqual(json.loads((backup_root / "ledger.json").read_text())["status"], "rolled-back-after-error")

    def test_restore_repair_backups_restores_sqlite_and_jsonl_after_completed_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-openai.jsonl"
            session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-openai", "model_provider": "openai"}})
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
                    "INSERT INTO threads VALUES ('thread-openai', 'openai', ?)",
                    (str(session_file),),
                )
                connection.commit()
            finally:
                connection.close()
            backup_root = codex_dir / "backup"

            result = repair_history_bucket(codex_dir, backup_root, "custom")
            self.assertEqual(result["status"], "completed")
            rollback = restore_repair_backups(codex_dir, backup_root)

            self.assertEqual(rollback["restored_state_backups"], 1)
            self.assertEqual(rollback["restored_jsonl_backups"], 1)
            connection = sqlite3.connect(state_path)
            try:
                provider = connection.execute(
                    "SELECT model_provider FROM threads WHERE id = 'thread-openai'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(provider, "openai")
            first_line = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first_line["payload"]["model_provider"], "openai")

    def test_normalize_history_provider_fast_replans_changed_session_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-openai.jsonl"
            session_file.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "thread-openai",
                            "model_provider": "openai",
                            "timestamp": "old",
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            original_plan = plan_session_file_provider_fast
            changed = False

            def mutate_after_plan(path: Path, active_codex_dir: Path, target_provider: str):
                nonlocal changed
                entry = original_plan(path, active_codex_dir, target_provider)
                if entry is not None and not changed:
                    changed = True
                    session_file.write_text(
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": "thread-openai",
                                    "model_provider": "openai",
                                    "timestamp": "new",
                                },
                            },
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                return entry

            with patch("history_overlay.plan_session_file_provider_fast", side_effect=mutate_after_plan):
                result = normalize_history_provider_fast(codex_dir, codex_dir / "backup", "custom")

            first_line = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first_line["payload"]["model_provider"], "custom")
            self.assertEqual(first_line["payload"]["timestamp"], "new")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["jsonl_applied"], 1)
            self.assertEqual(result["jsonl_skipped"], 0)
            self.assertEqual(result["jsonl"][0]["status"], "applied")
            self.assertEqual(json.loads(result["jsonl"][0]["old_first_line"])["payload"]["timestamp"], "new")

    def test_normalize_history_provider_fast_skips_missing_or_already_target_session_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            missing_file = sessions_dir / "rollout-missing.jsonl"
            target_file = sessions_dir / "rollout-target.jsonl"
            for path, provider in ((missing_file, "openai"), (target_file, "openai")):
                path.write_text(
                    json.dumps({"type": "session_meta", "payload": {"id": path.stem, "model_provider": provider}})
                    + "\n",
                    encoding="utf-8",
                )

            original_apply = apply_session_file_provider_fast

            def mutate_before_apply(entry: dict[str, str], active_codex_dir: Path):
                path = active_codex_dir / entry["path"]
                if path.name == "rollout-missing.jsonl":
                    path.unlink()
                if path.name == "rollout-target.jsonl":
                    path.write_text(
                        json.dumps({"type": "session_meta", "payload": {"id": path.stem, "model_provider": "custom"}})
                        + "\n",
                        encoding="utf-8",
                    )
                return original_apply(entry, active_codex_dir)

            with patch("history_overlay.apply_session_file_provider_fast", side_effect=mutate_before_apply):
                result = normalize_history_provider_fast(codex_dir, codex_dir / "backup", "custom")

            self.assertEqual(result["status"], "completed-with-skips")
            self.assertEqual(result["jsonl_applied"], 0)
            self.assertEqual(result["jsonl_skipped"], 2)
            reasons = {entry["reason"] for entry in result["jsonl"]}
            self.assertEqual(reasons, {"missing", "already_target"})

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

    def test_migrate_official_history_to_unified_requires_live_custom_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "rollout-openai.jsonl"
            session_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-openai", "model_provider": "openai"}})
                + "\n",
                encoding="utf-8",
            )

            result = migrate_official_history_to_unified(codex_dir, codex_dir / "backup")

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "live_not_unified")
            first_line = json.loads(session_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first_line["payload"]["model_provider"], "openai")

    def test_migrate_and_restore_official_history_uses_ledger_without_touching_new_custom_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            (codex_dir / "config.toml").write_text('model_provider = "custom"\n', encoding="utf-8")
            official_file = sessions_dir / "rollout-official.jsonl"
            official_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-official", "model_provider": "openai"}})
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
            state_path = codex_dir / "state_5.sqlite"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT)")
                connection.execute("INSERT INTO threads VALUES ('thread-official', 'openai')")
                connection.execute("INSERT INTO threads VALUES ('thread-existing-custom', 'custom')")
                connection.commit()
            finally:
                connection.close()

            backup_root = codex_dir / "backup"
            migrate_result = migrate_official_history_to_unified(codex_dir, backup_root)

            self.assertEqual(migrate_result["status"], "completed")
            self.assertEqual(migrate_result["state_rows"], 1)
            self.assertEqual(migrate_result["jsonl_applied"], 1)
            self.assertTrue((backup_root / "ledger.json").exists())
            self.assertTrue((backup_root / "jsonl" / "sessions" / "rollout-official.jsonl").exists())
            official_first = json.loads(official_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(official_first["payload"]["model_provider"], "custom")
            connection = sqlite3.connect(state_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-official"], "custom")
            self.assertEqual(providers["thread-existing-custom"], "custom")

            separated_inspection = inspect_unified_history_bucket(codex_dir, "openai", backup_root)
            self.assertEqual(separated_inspection["status"], "needs_repair")
            self.assertEqual(separated_inspection["dirty_state_rows"], 1)
            self.assertEqual(separated_inspection["dirty_jsonl_files"], 1)

            new_custom_file = sessions_dir / "rollout-new-custom.jsonl"
            new_custom_file.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "thread-new-custom", "model_provider": "custom"}})
                + "\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(state_path)
            try:
                connection.execute("INSERT INTO threads VALUES ('thread-new-custom', 'custom')")
                connection.commit()
            finally:
                connection.close()

            restore_result = restore_official_history_from_unified(codex_dir, backup_root)

            self.assertEqual(restore_result["status"], "completed")
            self.assertEqual(restore_result["state_rows"], 1)
            self.assertEqual(restore_result["jsonl_restored"], 1)
            official_first = json.loads(official_file.read_text(encoding="utf-8").splitlines()[0])
            existing_first = json.loads(existing_custom_file.read_text(encoding="utf-8").splitlines()[0])
            new_first = json.loads(new_custom_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(official_first["payload"]["model_provider"], "openai")
            self.assertEqual(existing_first["payload"]["model_provider"], "custom")
            self.assertEqual(new_first["payload"]["model_provider"], "custom")
            connection = sqlite3.connect(state_path)
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers["thread-official"], "openai")
            self.assertEqual(providers["thread-existing-custom"], "custom")
            self.assertEqual(providers["thread-new-custom"], "custom")
            self.assertEqual(inspect_unified_history_bucket(codex_dir, "openai", backup_root)["status"], "clean")

    def test_repair_history_bucket_can_restore_ledger_confirmed_custom_rows_to_openai(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            sessions_dir = codex_dir / "sessions"
            sessions_dir.mkdir(parents=True)
            official_file = sessions_dir / "rollout-official.jsonl"
            official_file.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "thread-official",
                            "model_provider": "custom",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            third_party_file = sessions_dir / "rollout-third-party.jsonl"
            third_party_file.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "thread-third-party",
                            "model_provider": "custom",
                        },
                    }
                )
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
                    "INSERT INTO threads VALUES ('thread-official', 'custom', 'gpt-5.5', ?)",
                    (str(official_file),),
                )
                connection.execute(
                    "INSERT INTO threads VALUES ('thread-third-party', 'custom', 'kimi-k2.7-code', ?)",
                    (str(third_party_file),),
                )
                connection.commit()
            finally:
                connection.close()

            ledger_root = codex_dir / "migration-ledger"
            ledger_root.mkdir()
            (ledger_root / "ledger.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mode": "migrate-official-to-unified",
                        "target_provider": "custom",
                        "created_at": "2026-07-04T00:00:00Z",
                        "codex_dir": str(codex_dir),
                        "state": [{"thread_ids": ["thread-official"]}],
                        "jsonl": [
                            {
                                "status": "applied",
                                "path": "sessions/rollout-official.jsonl",
                                "session_id": "thread-official",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = repair_history_bucket(codex_dir, codex_dir / "repair", "openai", ledger_root)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["state_rows"], 1)
            self.assertEqual(result["state_model_rows"], 0)
            self.assertEqual(result["jsonl_restored"], 1)
            connection = sqlite3.connect(state_path)
            try:
                rows = {
                    row[0]: (row[1], row[2])
                    for row in connection.execute("SELECT id, model_provider, model FROM threads")
                }
            finally:
                connection.close()
            self.assertEqual(rows["thread-official"], ("openai", "gpt-5.5"))
            self.assertEqual(rows["thread-third-party"], ("custom", "kimi-k2.7-code"))
            official_first = json.loads(official_file.read_text(encoding="utf-8").splitlines()[0])
            third_party_first = json.loads(third_party_file.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(official_first["payload"]["model_provider"], "openai")
            self.assertEqual(third_party_first["payload"]["model_provider"], "custom")


if __name__ == "__main__":
    unittest.main()
