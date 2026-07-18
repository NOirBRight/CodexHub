from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
import unittest

from history_consolidate import merge_global_state, official_main


def write_dead_legacy_lock(lock_path: Path) -> subprocess.Popen:
    """Write a legacy record whose PID is provably dead (recoverable).

    The returned process must stay referenced for the test duration: closing
    its handle would make the dead PID unresolvable on Windows.
    """
    child = subprocess.Popen([os.environ.get("PYTHON", "python"), "-c", "pass"])
    child_pid = child.pid
    assert child.wait(timeout=5) == 0
    lock_path.write_text(f"pid={child_pid}\nacquired_at_millis=0\n", encoding="utf-8")
    return child


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
            }
            official_state = {
                "thread-workspace-root-hints": {"thread-a": "C:/official", "thread-b": "C:/official"},
                "projectless-thread-ids": ["thread-b"],
                "selected-remote-host-id": "remote-ssh-discovered:am01s",
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
            self.assertNotIn("selected-remote-host-id", state)
            self.assertTrue((root / "backup" / "active-before").exists())

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
                        "electron-persisted-atom-state": {
                            "selected-remote-host-id": "remote-ssh-discovered:am01s",
                            "remote-connection-auto-connect-by-host-id": {"remote-ssh-discovered:am01s": True},
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = merge_global_state(active, official, root / "backup")

            self.assertEqual(result["copied"], 1)
            state = json.loads((active / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertNotIn("selected-remote-host-id", state)
            self.assertEqual(state["remote-connection-auto-connect-by-host-id"], {})
            self.assertNotIn("selected-remote-host-id", state["electron-persisted-atom-state"])
            self.assertEqual(state["electron-persisted-atom-state"]["remote-connection-auto-connect-by-host-id"], {})
            self.assertEqual(lock_path.read_text(encoding="ascii"), "codexhub-atomic-lock=1\n")


if __name__ == "__main__":
    unittest.main()
