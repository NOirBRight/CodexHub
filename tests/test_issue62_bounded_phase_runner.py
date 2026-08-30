from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_issue_62_live_control
from run_issue_62_live_control import (
    BoundedPhaseRunner,
    CandidateIdentity,
    CommandResult,
    HarnessFailure,
    _terminate_process,
)


IDENTITY = CandidateIdentity(
    candidate_sha="386168c945b95e3cdc1277151301dd6f236ff6e9",
    cli_version="0.146.0",
    cli_package_sha256="8050af14387e23b8d46026f023f0c1d33a2eefb39267bf36abe8cec2cec17b49",
    catalog_digest="e713f769e059423784ca1f689f34957f764eedd8439f036d50c09b65c32ee7a6",
    route_digest="a" * 64,
)


class _OsProxy:
    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


def _patch_windows_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_issue_62_live_control, "os", _OsProxy("nt"))


def _records(root: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (root / "phase-status.jsonl").read_text().splitlines()]


def test_success_writes_only_sanitized_phase_status_and_cleanup_receipt(tmp_path: Path) -> None:
    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    result = runner.run_subprocess(
        "catalog_sync", [sys.executable, "-c", "print('secret must not be captured')"]
    )

    assert result.status == "completed"
    receipt = runner.cleanup()
    assert receipt["cleanup_completed"] is True
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "cleanup-receipt.json",
        "phase-status.jsonl",
    ]
    markers = _records(tmp_path)
    assert [record["marker"] for record in markers[:2]] == [
        "catalog_sync_started",
        "catalog_sync_completed",
    ]
    assert all("secret" not in json.dumps(record) for record in markers)
    assert all("argv" not in record and "command" not in record for record in markers)


class _FakeProcess:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code
        self.pid = 12345
        self.terminated = False
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        if self.return_code is None:
            raise TimeoutError
        return self.return_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.terminated = True
        self.return_code = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.terminated = True
        self.return_code = -9

    def mark_tree_terminated(self) -> None:
        self.terminated = True
        self.return_code = 0


class _BlockingFakeProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__(return_code=None)
        self.spawned = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self.spawned.set()
        time.sleep(0.005)
        if self.return_code is None:
            raise subprocess.TimeoutExpired("fixture", timeout or 0)
        return self.return_code


def _patch_successful_tree_termination(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeProcess,
) -> list[list[str]]:
    taskkill_calls: list[list[str]] = []
    if os.name == "nt":

        def fake_taskkill(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            taskkill_calls.append(argv)
            process.mark_tree_terminated()
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr("run_issue_62_live_control.subprocess.run", fake_taskkill)
    else:
        monkeypatch.setattr(
            "run_issue_62_live_control.os.killpg",
            lambda pid, sig: process.terminate(),
        )
    return taskkill_calls


def test_timeout_is_terminal_and_cleanup_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess()

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("run_issue_62_live_control.subprocess.Popen", fake_popen)
    # Move the deadline forward without sleeping for 30 seconds.
    clock = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(
        "run_issue_62_live_control.time.monotonic",
        lambda: next(clock, 31.0),
    )

    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    taskkill_calls = _patch_successful_tree_termination(monkeypatch, process)
    result = runner.run_subprocess("cli", ["fixture", "secret"])
    assert result.status == "timed_out"
    assert process.terminated is True
    if os.name == "nt":
        assert taskkill_calls == [["taskkill", "/PID", "12345", "/T", "/F"]]
        assert process.kill_calls == 0
    else:
        assert process.terminate_calls == 1
    records = _records(tmp_path)
    assert records[-1]["status_code"] == "phase_timeout"
    assert runner.cleanup()["cleanup_completed"] is True


def test_cancellation_terminates_process_and_writes_cancelled_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("run_issue_62_live_control.subprocess.Popen", fake_popen)
    cancel = threading.Event()
    cancel.set()
    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    result = runner.run_subprocess("cli", ["fixture"], cancellation_event=cancel)
    assert result.status == "cancelled"
    assert process.terminated is False  # cancellation before spawn is fail-closed
    assert _records(tmp_path)[-1]["status_code"] == "cancelled"
    assert runner.cleanup()["cleanup_completed"] is True


def test_cancellation_of_active_process_terminates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _BlockingFakeProcess()
    monkeypatch.setattr(
        "run_issue_62_live_control.subprocess.Popen", lambda *args, **kwargs: process
    )
    cancel = threading.Event()
    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    taskkill_calls = _patch_successful_tree_termination(monkeypatch, process)
    result_box: list[CommandResult] = []

    worker = threading.Thread(
        target=lambda: result_box.append(
            runner.run_subprocess("cli", ["fixture"], cancellation_event=cancel)
        )
    )
    worker.start()
    assert process.spawned.wait(1)
    cancel.set()
    worker.join(2)
    assert not worker.is_alive()
    assert result_box[0].status == "cancelled"
    assert process.terminated is True
    if os.name == "nt":
        assert taskkill_calls == [["taskkill", "/PID", "12345", "/T", "/F"]]
        assert process.kill_calls == 0
    else:
        assert process.terminate_calls == 1
    assert runner.cleanup()["cleanup_completed"] is True


def test_stale_or_extra_artifact_is_rejected_before_start(tmp_path: Path) -> None:
    (tmp_path / "stale.json").write_text("{}", encoding="utf-8")
    with pytest.raises(HarnessFailure) as error:
        BoundedPhaseRunner(tmp_path, IDENTITY)
    assert str(error.value) == "stale_artifact"


def test_extra_artifact_after_start_makes_cleanup_fail_closed(tmp_path: Path) -> None:
    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    result = runner.run_subprocess("catalog_sync", [sys.executable, "-c", "pass"])
    assert result.status == "completed"
    (tmp_path / "unexpected.bin").write_bytes(b"not evidence")

    receipt = runner.cleanup()
    assert receipt["cleanup_completed"] is False
    assert receipt["resources_released"] is False
    assert receipt["cleanup_failure_count"] == 1
    assert _records(tmp_path)[-1]["status_code"] == "cleanup_incomplete"


def test_nested_extra_artifact_is_rejected(tmp_path: Path) -> None:
    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    result = runner.run_subprocess("catalog_sync", [sys.executable, "-c", "pass"])
    assert result.status == "completed"
    nested = tmp_path / "unexpected"
    nested.mkdir()
    (nested / "raw-body.bin").write_bytes(b"body")

    receipt = runner.cleanup()
    assert receipt["cleanup_completed"] is False
    assert receipt["cleanup_failure_count"] == 1


def test_windows_cleanup_uses_taskkill_tree_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _BlockingFakeProcess()
    calls: list[tuple[object, dict[str, object]]] = []
    _patch_windows_platform(monkeypatch)

    def fake_taskkill(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls.append((args[0], kwargs))
        process.mark_tree_terminated()
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr("run_issue_62_live_control.subprocess.run", fake_taskkill)

    assert _terminate_process(process) is True
    assert calls[0][0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert process.kill_calls == 0


@pytest.mark.parametrize("failure", ["nonzero", "exception"])
def test_windows_cleanup_fails_closed_when_taskkill_tree_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    process = _FakeProcess()
    _patch_windows_platform(monkeypatch)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    monkeypatch.setattr(
        "run_issue_62_live_control.subprocess.Popen",
        lambda *args, **kwargs: process,
    )

    if failure == "exception":

        def fail_taskkill(*args: object, **kwargs: object) -> None:
            raise OSError("taskkill unavailable")
    else:

        def fail_taskkill(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
            return subprocess.CompletedProcess(args[0], 1)

    monkeypatch.setattr("run_issue_62_live_control.subprocess.run", fail_taskkill)
    clock = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(
        "run_issue_62_live_control.time.monotonic",
        lambda: next(clock, 31.0),
    )

    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    result = runner.run_subprocess("cli", ["fixture"])

    assert result.status == "timed_out"
    assert result.status_code == "termination_failed"
    assert result.terminated is False
    assert process.kill_calls == 0
    receipt = runner.cleanup()
    assert receipt["cleanup_completed"] is False
    assert receipt["child_processes_terminated"] is False
