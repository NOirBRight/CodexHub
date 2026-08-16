"""Shared fixtures for stale-lock recovery tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def write_dead_legacy_lock(lock_path: Path) -> subprocess.Popen:
    """Write a legacy record whose PID is provably dead (recoverable).

    The returned process must stay referenced for the test duration: closing
    its handle would make the dead PID unresolvable on Windows.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child_pid = child.pid
    assert child.wait(timeout=5) == 0
    lock_path.write_text(f"pid={child_pid}\nacquired_at_millis=0\n", encoding="utf-8")
    return child
