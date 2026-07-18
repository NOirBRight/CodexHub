from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
import unittest

from history_consolidate import (
    canonicalize_workspace_root,
    import_saved_workspace_roots,
    merge_global_state,
    official_main,
    preview_saved_workspace_roots,
    status,
)
from lock_fixtures import write_dead_legacy_lock


def write_session(path: Path, thread_id: str, provider: str, markers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": thread_id, "model_provider": provider}}, separators=(",", ":"))
    ]
    lines.extend(json.dumps({"type": "event_msg", "payload": {"message": marker}}, separators=(",", ":")) for marker in markers)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_state(path: Path, rows: list[tuple[str, str, str]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, title TEXT)")
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()


def create_state_with_columns(path: Path, columns: list[str], rows: list[tuple[object, ...]]) -> None:
    connection = sqlite3.connect(path)
    try:
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(f"CREATE TABLE threads ({column_sql})")
        connection.executemany(f"INSERT INTO threads VALUES ({placeholders})", rows)
        connection.commit()
    finally:
        connection.close()


class HistoryConsolidateTests(unittest.TestCase):
    def test_default_global_merge_preserves_saved_workspace_roots_without_importing_source_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active = root / ".codex"
            official = root / "official"
            active.mkdir(parents=True)
            official.mkdir(parents=True)
            (active / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "electron-saved-workspace-roots": ["C:/active/project"],
                        "project-order": ["active-project"],
                    }
                ),
                encoding="utf-8",
            )
            (official / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "electron-saved-workspace-roots": [
                            "C:/official/stale-project",
                            "C:/active/project",
                        ],
                        "project-order": ["official-project"],
                    }
                ),
                encoding="utf-8",
            )

            merge_global_state(active, official, root / "backup")

            state = json.loads((active / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["electron-saved-workspace-roots"], ["C:/active/project"])
            self.assertEqual(state["project-order"], ["active-project", "official-project"])

    def test_explicit_workspace_root_import_previews_canonical_deduplicated_additions_and_backups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active = root / ".codex"
            official = root / "official"
            active.mkdir(parents=True)
            official.mkdir(parents=True)
            active_roots = [r"C:\Users\Noir\Project", "C:/active/second"]
            source_roots = [
                r"c:\users\noir\project\.",
                "C:/source/z/.",
                "c:/source/A",
                "C:/source/a/",
                "C:/source/z",
            ]
            active_state = {"electron-saved-workspace-roots": active_roots, "project-order": ["active"]}
            source_state = {"electron-saved-workspace-roots": source_roots, "project-order": ["official"]}
            active_state_path = active / ".codex-global-state.json"
            active_state_path.write_text(json.dumps(active_state), encoding="utf-8")
            (official / ".codex-global-state.json").write_text(json.dumps(source_state), encoding="utf-8")

            preview = preview_saved_workspace_roots(active_state, source_state)
            expected_added = sorted(
                {
                    canonicalize_workspace_root(value)
                    for value in source_roots
                    if canonicalize_workspace_root(value) is not None
                }
                - {
                    canonicalize_workspace_root(value)
                    for value in active_roots
                    if canonicalize_workspace_root(value) is not None
                }
            )
            self.assertEqual(preview["active_roots"], active_roots)
            self.assertEqual(preview["added_roots"], expected_added)

            backup = root / "backup"
            preview_result = import_saved_workspace_roots(active, official, backup)
            self.assertFalse(preview_result["applied"])
            self.assertFalse(backup.exists())

            applied = import_saved_workspace_roots(active, official, backup, apply=True)
            self.assertTrue(applied["applied"])
            self.assertTrue(applied["backup_created"])
            state = json.loads(active_state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["electron-saved-workspace-roots"], active_roots + expected_added)
            backup_state = json.loads(
                (backup / "active-before" / ".codex-global-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(backup_state, active_state)

    def test_workspace_root_import_ignores_source_without_workspace_key_and_status_reports_active_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active = root / ".codex"
            official = root / "official"
            active.mkdir(parents=True)
            official.mkdir(parents=True)
            active_roots = ["C:/active/project"]
            (active / ".codex-global-state.json").write_text(
                json.dumps({"electron-saved-workspace-roots": active_roots}), encoding="utf-8"
            )
            (official / ".codex-global-state.json").write_text(
                json.dumps({"project-order": ["official"]}), encoding="utf-8"
            )

            result = import_saved_workspace_roots(active, official, root / "backup", apply=True)

            self.assertEqual(result["preview"]["added_roots"], [])
            self.assertFalse(result["preview"]["source_key_present"])
            self.assertEqual(
                json.loads((active / ".codex-global-state.json").read_text(encoding="utf-8"))[
                    "electron-saved-workspace-roots"
                ],
                active_roots,
            )
            self.assertFalse((root / "backup").exists())
            self.assertEqual(status(active)["saved_workspace_roots"]["active_roots"], active_roots)

    def test_official_main_dry_run_reports_saved_root_policy_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active = root / ".codex"
            official = root / "official"
            active.mkdir(parents=True)
            official.mkdir(parents=True)
            active_state = {"electron-saved-workspace-roots": ["C:/active/project"]}
            source_state = {"electron-saved-workspace-roots": ["C:/official/project"]}
            active_state_path = active / ".codex-global-state.json"
            active_state_path.write_text(json.dumps(active_state), encoding="utf-8")
            (official / ".codex-global-state.json").write_text(json.dumps(source_state), encoding="utf-8")

            result = official_main(active, official, root / "backup", "custom", dry_run=True)

            self.assertTrue(result["dry_run"])
            self.assertFalse(result["global_state"]["saved_workspace_roots"]["applied"])
            self.assertEqual(json.loads(active_state_path.read_text(encoding="utf-8")), active_state)
            self.assertFalse((root / "backup").exists())

    def test_official_main_merges_source_branch_then_active_tail_and_normalizes_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active = root / ".codex"
            official = root / "official"
            active_file = active / "sessions" / "2026" / "06" / "18" / "rollout-thread-a.jsonl"
            official_file = official / "sessions" / "2026" / "06" / "18" / "rollout-thread-a.jsonl"
            write_session(active_file, "thread-a", "custom", ["common", "active-tail"])
            write_session(official_file, "thread-a", "openai", ["common", "official-main"])
            write_session(official / "sessions" / "rollout-thread-b.jsonl", "thread-b", "openai", ["official-only"])
            write_session(active / "sessions" / "rollout-thread-c.jsonl", "thread-c", "openai", ["active-only"])

            create_state(active / "state_5.sqlite", [("thread-a", "custom", "active"), ("thread-c", "openai", "active-only")])
            create_state(official / "state_5.sqlite", [("thread-a", "openai", "official"), ("thread-b", "openai", "official-only")])

            active_state = {
                "thread-workspace-root-hints": {"thread-c": "C:/active"},
                "projectless-thread-ids": ["thread-c"],
                "electron-saved-workspace-roots": ["C:/active/project"],
            }
            official_state = {
                "thread-workspace-root-hints": {"thread-a": "C:/official", "thread-b": "C:/official"},
                "projectless-thread-ids": ["thread-b"],
                "selected-remote-host-id": "remote-ssh-discovered:am01s",
                "electron-saved-workspace-roots": ["C:/official/stale-project"],
                "skills": {"must-not-copy": True},
            }
            (active / ".codex-global-state.json").write_text(json.dumps(active_state), encoding="utf-8")
            (official / ".codex-global-state.json").write_text(json.dumps(official_state), encoding="utf-8")

            result = official_main(active, official, root / "backup", "custom")

            self.assertEqual(result["jsonl"]["merged-branch"], 1)
            merged = [json.loads(line) for line in active_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(merged[0]["payload"]["model_provider"], "custom")
            self.assertEqual([line["payload"]["message"] for line in merged[1:]], ["common", "official-main", "active-tail"])
            copied = [json.loads(line) for line in (active / "sessions" / "rollout-thread-b.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(copied[0]["payload"]["model_provider"], "custom")
            active_only = [json.loads(line) for line in (active / "sessions" / "rollout-thread-c.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(active_only[0]["payload"]["model_provider"], "custom")

            connection = sqlite3.connect(active / "state_5.sqlite")
            try:
                providers = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(providers, {"thread-a": "custom", "thread-b": "custom", "thread-c": "custom"})

            state = json.loads((active / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["thread-workspace-root-hints"]["thread-b"], "C:/official")
            self.assertEqual(state["electron-saved-workspace-roots"], ["C:/active/project"])
            self.assertNotIn("skills", state)
            self.assertNotIn("selected-remote-host-id", state)
            self.assertTrue((root / "backup" / "active-before").exists())
            self.assertFalse(result["global_state"]["saved_workspace_roots"]["applied"])
            self.assertEqual(
                result["global_state"]["saved_workspace_roots"]["added_roots"],
                ["c:/official/stale-project"],
            )

    def test_official_main_inserts_source_threads_when_state_schema_order_differs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active = root / ".codex"
            official = root / "official"
            write_session(active / "sessions" / "rollout-thread-a.jsonl", "thread-a", "custom", ["active"])
            write_session(official / "sessions" / "rollout-thread-b.jsonl", "thread-b", "openai", ["official-only"])

            create_state_with_columns(
                active / "state_5.sqlite",
                ["id TEXT PRIMARY KEY", "model_provider TEXT", "title TEXT"],
                [("thread-a", "custom", "active")],
            )
            create_state_with_columns(
                official / "state_5.sqlite",
                ["title TEXT", "extra TEXT", "model_provider TEXT", "id TEXT PRIMARY KEY"],
                [("official-only", "ignored", "openai", "thread-b")],
            )

            official_main(active, official, root / "backup", "custom")

            connection = sqlite3.connect(active / "state_5.sqlite")
            try:
                rows = dict(connection.execute("SELECT id, model_provider FROM threads"))
            finally:
                connection.close()
            self.assertEqual(rows["thread-b"], "custom")

    def test_merge_global_state_sanitizes_remote_selection_when_copying_missing_active_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active = root / ".codex"
            official = root / "official"
            active.mkdir(parents=True)
            official.mkdir(parents=True)
            active_state_path = active / ".codex-global-state.json"
            lock_path = active_state_path.with_name(".codex-global-state.json.lock")
            _dead_child = write_dead_legacy_lock(lock_path)
            (official / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "selected-remote-host-id": "remote-ssh-discovered:am01s",
                        "remote-connection-auto-connect-by-host-id": {"remote-ssh-discovered:am01s": True},
                        "electron-saved-workspace-roots": ["C:/stale/project"],
                        "electron-persisted-atom-state": {
                            "selected-remote-host-id": "remote-ssh-discovered:am01s",
                            "remote-connection-auto-connect-by-host-id": {"remote-ssh-discovered:am01s": True},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (official / "skills").mkdir()
            (official / "skills" / "must-not-copy.md").write_text("skill data", encoding="utf-8")
            (official / "plugins").mkdir()
            (official / "plugins" / "must-not-copy.json").write_text("plugin data", encoding="utf-8")

            result = merge_global_state(active, official, root / "backup")

            self.assertEqual(result["copied"], 1)
            state = json.loads((active / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertNotIn("selected-remote-host-id", state)
            self.assertNotIn("electron-saved-workspace-roots", state)
            self.assertEqual(state["remote-connection-auto-connect-by-host-id"], {})
            self.assertNotIn("selected-remote-host-id", state["electron-persisted-atom-state"])
            self.assertEqual(state["electron-persisted-atom-state"]["remote-connection-auto-connect-by-host-id"], {})
            self.assertFalse((active / "skills").exists())
            self.assertFalse((active / "plugins").exists())
            self.assertEqual(lock_path.read_text(encoding="ascii"), "codexhub-atomic-lock=1\n")


if __name__ == "__main__":
    unittest.main()
