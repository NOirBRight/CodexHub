"""Small, fixed-version test evidence recorder used by the real-client fixture.

The recorder deliberately lives outside the model-controlled fixture directory.
It appends structured events from the real unittest process and never trusts
stdout, a sentinel, or a model-authored result file as proof of execution.
"""
from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import atexit
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import unittest
import hashlib


SCHEMA = "codexhub.e2e-test-evidence.v1"
_LOCK = threading.Lock()
_PATH: Path | None = None
_EXPECTED: tuple[str, ...] = ()
_EVENTS: list[dict[str, object]] = []
_FINISHED = False


def _recorder_identity() -> tuple[str, str | None]:
    """Return the loaded recorder path and its source digest.

    The fixture is model-controlled, but this module is deliberately loaded
    from the harness' fixed ``scripts`` directory.  Recording the identity in
    every event lets the parent-side verifier distinguish that module from a
    fixture file with the same import name.  The process observer still has to
    prove that the reported module was opened by the test process.
    """
    path = Path(__file__).resolve()
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    return str(path), digest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path() -> Path | None:
    global _PATH
    if _PATH is None:
        value = os.environ.get("CODEXHUB_E2E_TEST_EVIDENCE")
        if value:
            _PATH = Path(value).resolve()
    return _PATH


def _append(event: dict[str, object]) -> None:
    path = _path()
    if path is None:
        return
    recorder_path, recorder_sha256 = _recorder_identity()
    event = {
        "schema": SCHEMA,
        "timestamp": _now(),
        "monotonic_ns": time.monotonic_ns(),
        "pid": os.getpid(),
        "recorder_path": recorder_path,
        "recorder_sha256": recorder_sha256,
        **event,
    }
    with _LOCK:
        _EVENTS.append(event)
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND makes an interrupted or concurrent writer observable rather
        # than allowing a later event to replace the earlier evidence.
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _digest_files(root: Path, names: list[str]) -> str | None:
    digest = hashlib.sha256()
    for name in names:
        if not isinstance(name, str) or not name:
            return None
        value = _digest(root / name)
        if value is None:
            return None
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _fixture_digests() -> dict[str, str | None]:
    source_name = os.environ.get("CODEXHUB_E2E_SOURCE_FILE", "parent_task.py")
    raw_test_names = os.environ.get("CODEXHUB_E2E_TEST_FILES")
    test_names = raw_test_names.split(os.pathsep) if raw_test_names else []
    if not test_names and os.environ.get("CODEXHUB_E2E_TEST_FILE"):
        test_names = [os.environ["CODEXHUB_E2E_TEST_FILE"]]
    return {
        "source_sha": _digest(Path.cwd() / source_name),
        "test_sha": _digest_files(Path.cwd(), test_names) if test_names else None,
    }


def register_suite(expected_cases: list[str] | tuple[str, ...]) -> None:
    """Register the immutable expected case set before unittest discovers it."""
    global _EXPECTED
    _EXPECTED = tuple(expected_cases)
    _append({"event": "suite_started", "expected_cases": list(_EXPECTED), **_fixture_digests()})


def case_started(case_id: str) -> None:
    _append({"event": "case_started", "case": case_id})


def case_finished(case_id: str, outcome: str) -> None:
    _append({"event": "case_finished", "case": case_id, "outcome": outcome})


def _finish() -> None:
    global _FINISHED
    if _FINISHED:
        return
    _FINISHED = True
    outcomes: dict[str, str] = {}
    for event in _EVENTS:
        if event.get("event") == "case_finished" and isinstance(event.get("case"), str):
            outcomes[event["case"]] = str(event.get("outcome"))
    _append({
        "event": "suite_finished",
        "expected_cases": list(_EXPECTED),
        "case_outcomes": outcomes,
        "complete": set(_EXPECTED).issubset(outcomes) and all(
            outcomes.get(case) == "passed" for case in _EXPECTED
        ),
        **_fixture_digests(),
    })


atexit.register(_finish)


class EvidenceTestCase(unittest.TestCase):
    """Base class that records each actual unittest case and its result."""

    def run(self, result: unittest.TestResult | None = None):  # type: ignore[override]
        case_id = self.id()
        case_started(case_id)
        result = super().run(result)
        outcome = "passed"
        if result is not None:
            errors = {test.id() for test, _ in result.errors}
            errors.update(test.id() for test, _ in result.failures)
            skipped = {test.id() for test, _ in result.skipped}
            if case_id in errors:
                outcome = "failed"
            elif case_id in skipped:
                outcome = "skipped"
        case_finished(case_id, outcome)
        return result


__all__ = ["EvidenceTestCase", "SCHEMA", "case_finished", "case_started", "register_suite"]
