from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from global_state_repair import repair_global_state


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


class GlobalStateRepairTests(unittest.TestCase):
    def test_repair_removes_remote_selection_but_preserves_project_state(self):
        original = {
            "selected-remote-host-id": "remote-control:env_123",
            "remote-connection-auto-connect-by-host-id": {"remote-control:env_123": True},
            "project-order": ["\\\\?\\D:\\Workstation\\pi-gsd"],
            "projectless-thread-ids": ["thread-a"],
            "thread-workspace-root-hints": {"thread-a": "\\\\?\\C:\\Users\\noirb\\Documents\\Codex"},
            "electron-persisted-atom-state": {
                "selected-remote-host-id": "remote-control:env_123",
                "active-remote-workspace-root": "\\\\?\\D:\\Workstation\\pi-gsd",
                "some-local-key": "keep",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / ".codex-global-state.json"
            backup_path = tmp / "backup" / ".codex-global-state.json"
            state_path.write_text(json.dumps(original), encoding="utf-8")

            result = repair_global_state(state_path, backup_path)
            repaired = json.loads(state_path.read_text(encoding="utf-8"))
            backup = json.loads(backup_path.read_text(encoding="utf-8"))

        self.assertTrue(result["changed"])
        self.assertEqual(backup["selected-remote-host-id"], "remote-control:env_123")
        self.assertNotIn("selected-remote-host-id", repaired)
        self.assertEqual(repaired["remote-connection-auto-connect-by-host-id"], {})
        self.assertEqual(repaired["project-order"], ["\\\\?\\D:\\Workstation\\pi-gsd"])
        self.assertEqual(repaired["projectless-thread-ids"], ["thread-a"])
        self.assertEqual(
            repaired["thread-workspace-root-hints"],
            {"thread-a": "\\\\?\\C:\\Users\\noirb\\Documents\\Codex"},
        )
        self.assertNotIn("selected-remote-host-id", repaired["electron-persisted-atom-state"])
        self.assertNotIn("active-remote-workspace-root", repaired["electron-persisted-atom-state"])
        self.assertEqual(repaired["electron-persisted-atom-state"]["some-local-key"], "keep")

    def test_repair_is_noop_when_no_remote_selection_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / ".codex-global-state.json"
            backup_path = tmp / "backup" / ".codex-global-state.json"
            state_path.write_text('{"project-order":[]}', encoding="utf-8")

            result = repair_global_state(state_path, backup_path)

            self.assertFalse(result["changed"])
            self.assertFalse(backup_path.exists())

    def test_repair_write_recovers_stale_atomic_lock(self):
        original = {
            "selected-remote-host-id": "remote-control:env_123",
            "project-order": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / ".codex-global-state.json"
            backup_path = tmp / "backup" / ".codex-global-state.json"
            state_path.write_text(json.dumps(original), encoding="utf-8")
            lock_path = state_path.with_name(".codex-global-state.json.lock")
            _dead_child = write_dead_legacy_lock(lock_path)

            result = repair_global_state(state_path, backup_path)

            self.assertTrue(result["changed"])
            self.assertEqual(lock_path.read_text(encoding="ascii"), "codexhub-atomic-lock=1\n")
            self.assertNotIn(
                "selected-remote-host-id",
                json.loads(state_path.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
