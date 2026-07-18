import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bucket_sync import sync_dir


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


class BucketSyncTests(unittest.TestCase):
    def test_keeps_destination_when_it_extends_source_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "session.jsonl").write_bytes(b"a\nb\n")
            (destination / "session.jsonl").write_bytes(b"a\nb\nc\n")

            counts = sync_dir(source, destination)

            self.assertEqual((destination / "session.jsonl").read_bytes(), b"a\nb\nc\n")
            self.assertEqual(counts, {"kept-destination": 1})

    def test_replaces_destination_when_source_extends_it_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "session.jsonl").write_bytes(b"a\nb\nc\n")
            (destination / "session.jsonl").write_bytes(b"a\nb\n")

            counts = sync_dir(source, destination)

            self.assertEqual((destination / "session.jsonl").read_bytes(), b"a\nb\nc\n")
            self.assertEqual(counts, {"merged": 1})

    def test_merges_diverged_jsonl_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "session.jsonl").write_bytes(b"a\nsource-new\n")
            (destination / "session.jsonl").write_bytes(b"a\ndestination-old\n")

            counts = sync_dir(source, destination)

            self.assertEqual(
                (destination / "session.jsonl").read_bytes(),
                b"a\ndestination-old\nsource-new\n",
            )
            self.assertEqual(counts, {"merged": 1})

    def test_merges_diverged_jsonl_without_repeating_overlapping_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "session.jsonl").write_bytes(b"a\nactive-old\nactive-new\n")
            (destination / "session.jsonl").write_bytes(
                b"a\nbucket-only\nactive-old\n"
            )

            counts = sync_dir(source, destination)

            self.assertEqual(
                (destination / "session.jsonl").read_bytes(),
                b"a\nbucket-only\nactive-old\nactive-new\n",
            )
            self.assertEqual(counts, {"merged": 1})

    def test_merged_jsonl_write_recovers_stale_atomic_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "session.jsonl").write_bytes(b"a\nb\nc\n")
            target = destination / "session.jsonl"
            target.write_bytes(b"a\nb\n")
            lock_path = target.with_name("session.jsonl.lock")
            _dead_child = write_dead_legacy_lock(lock_path)

            counts = sync_dir(source, destination)

            self.assertEqual(target.read_bytes(), b"a\nb\nc\n")
            self.assertEqual(counts, {"merged": 1})
            self.assertEqual(lock_path.read_text(encoding="ascii"), "codexhub-atomic-lock=1\n")

    def test_non_jsonl_overwrite_recovers_stale_atomic_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "state.json").write_bytes(b'{"new":true}\n')
            target = destination / "state.json"
            target.write_bytes(b'{"old":true}\n')
            lock_path = target.with_name("state.json.lock")
            _dead_child = write_dead_legacy_lock(lock_path)

            counts = sync_dir(source, destination)

            self.assertEqual(target.read_bytes(), b'{"new":true}\n')
            self.assertEqual(counts, {"overwritten": 1})
            self.assertEqual(lock_path.read_text(encoding="ascii"), "codexhub-atomic-lock=1\n")


if __name__ == "__main__":
    unittest.main()
