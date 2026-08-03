from __future__ import annotations

import json
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


def test_direct_runner_drops_sensitive_and_host_path_overrides(tmp_path: Path) -> None:
    runner = BoundedPhaseRunner(
        tmp_path,
        IDENTITY,
        environment={
            "HOME": "C:/host/home",
            "OPENAI_API_KEY": "must-not-pass",
            "PATH": "C:/host/bin",
            "PYTHONPATH": "../host-modules",
        },
        working_directory=tmp_path,
    )

    isolated_home_keys = (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
    )
    isolated_home_paths = {
        key: Path(runner._environment[key]).resolve()
        for key in isolated_home_keys
    }
    assert len(set(isolated_home_paths.values())) == len(isolated_home_keys)
    assert all(path.is_relative_to(tmp_path.resolve()) for path in isolated_home_paths.values())
    assert all("host" not in str(path).lower() for path in isolated_home_paths.values())
    assert "OPENAI_API_KEY" not in runner._environment
    assert "PATH" not in runner._environment
    assert "PYTHONPATH" not in runner._environment
    assert runner.cleanup()["cleanup_completed"] is True


class _FakeProcess:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code
        self.pid = 12345
        self.terminated = False

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        if self.return_code is None:
            raise TimeoutError
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def kill(self) -> None:
        self.terminated = True
        self.return_code = -9


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


class _ExitedParentWithDescendant(_FakeProcess):
    """A root whose PID is exited while its tree still needs teardown."""

    def __init__(self) -> None:
        super().__init__(return_code=0)
        self.tree_stop_attempted = False


def test_exited_root_still_attempts_tree_stop_and_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _ExitedParentWithDescendant()
    calls: list[tuple[object, dict[str, object]]] = []
    readback: list[int] = []
    monkeypatch.setattr("run_issue_62_live_control.os.name", "nt")
    monkeypatch.setattr(
        "run_issue_62_live_control.subprocess.run",
        lambda *args, **kwargs: (
            calls.append((args[0], kwargs)) or type("Result", (), {"returncode": 0})()
        ),
    )
    monkeypatch.setattr(
        "run_issue_62_live_control._process_tree_pids",
        lambda root_pid: {int(root_pid), 54321},
    )
    monkeypatch.setattr(
        "run_issue_62_live_control._process_tree_is_gone",
        lambda tracked_process, tracked_pids: readback.append(int(tracked_process.pid)) or True,
    )

    assert _terminate_process(process) is True
    assert calls[0][0] == ["taskkill", "/PID", "54321", "/F"]
    assert readback == [12345]


def test_tree_readback_failure_cannot_report_cleanup_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _ExitedParentWithDescendant()
    monkeypatch.setattr("run_issue_62_live_control.os.name", "nt")
    monkeypatch.setattr(
        "run_issue_62_live_control.subprocess.run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(
        "run_issue_62_live_control._process_tree_is_gone",
        lambda tracked_process, tracked_pids: False,
    )

    assert _terminate_process(process) is False


def test_cleanup_rechecks_completed_foreground_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess(return_code=0)
    monkeypatch.setattr(
        "run_issue_62_live_control.subprocess.Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        "run_issue_62_live_control._process_tree_is_gone",
        lambda tracked_process, tracked_pids: False,
    )

    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    result = runner.run_subprocess("cli", ["fixture"])
    assert result.status == "completed"

    receipt = runner.cleanup()
    assert receipt["cleanup_completed"] is False
    assert receipt["child_processes_terminated"] is False


def test_exited_parent_with_live_descendant_is_cleaned_before_success(
    tmp_path: Path,
) -> None:
    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    probe_root = Path(runner._environment["HOME"])
    survivor_marker = probe_root / "survivor-marker"
    pid_file = probe_root / "descendant.pid"
    grandchild_code = (
        "import pathlib, time; "
        "time.sleep(1.5); "
        f"pathlib.Path({str(survivor_marker)!r}).write_text('survived', encoding='ascii')"
    )
    parent_code = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "close_fds=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')"
    )
    handle = runner.start_background(
        "descendant",
        [sys.executable, "-c", parent_code, str(pid_file), grandchild_code],
    )
    process = handle.process
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        process.wait(timeout=5)

        assert _terminate_process(process, tracked_pids=set(handle.tracked_pids)) is True
        time.sleep(2)
        assert not survivor_marker.exists()
    finally:
        if process.poll() is None:
            _terminate_process(process, tracked_pids=set(handle.tracked_pids))
        runner.cleanup()


def test_timeout_is_terminal_and_cleanup_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess()

    def fake_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr("run_issue_62_live_control.subprocess.Popen", fake_popen)
    monkeypatch.setattr("run_issue_62_live_control.subprocess.run", lambda *args, **kwargs: None)
    # Move the deadline forward without sleeping for 30 seconds.
    clock = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(
        "run_issue_62_live_control.time.monotonic",
        lambda: next(clock, 31.0),
    )

    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
    result = runner.run_subprocess("cli", ["fixture", "secret"])
    assert result.status == "timed_out"
    assert process.terminated is True
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
    monkeypatch.setattr("run_issue_62_live_control.subprocess.run", lambda *args, **kwargs: None)
    cancel = threading.Event()
    runner = BoundedPhaseRunner(tmp_path, IDENTITY)
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
    monkeypatch.setattr("run_issue_62_live_control.os.name", "nt")
    monkeypatch.setattr(
        "run_issue_62_live_control.subprocess.run",
        lambda *args, **kwargs: calls.append((args[0], kwargs)),
    )

    assert _terminate_process(process) is True
    assert calls[0][0] == ["taskkill", "/PID", "12345", "/T", "/F"]
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
