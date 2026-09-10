from pathlib import Path
import importlib.util
import json


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/collaboration_process_evidence.py"
    spec = importlib.util.spec_from_file_location("process_evidence", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_actual_python_test_process_not_shell_spelling(tmp_path):
    m = module()
    trace = tmp_path / "trace.101"
    trace.write_text('execve("/usr/bin/python", ["python", "-m", "unittest", "test_parent_task.py", "-q"], 0x1 /* 2 vars */) = 0\n'
                     'openat(AT_FDCWD, "test_parent_task.py", O_RDONLY) = 3</fixture/test_parent_task.py>\n'
                     'exit_group(0) = ?\n')
    evidence = m.read_process_evidence(tmp_path / "trace", Path("/fixture"), Path("/usr/bin/python"))
    assert evidence["tests"][0]["pid"] == 101
    assert evidence["tests"][0]["test"] == "test_parent_task.py"


def test_echo_or_wrong_exit_cannot_prove_test_execution(tmp_path):
    for name, executable, code in (("101", "/bin/echo", 0), ("102", "/usr/bin/python", 1)):
        (tmp_path / ("trace." + name)).write_text(
            f'execve("{executable}", ["python", "-m", "unittest", "test_parent_task.py"], 0x1) = 0\n'
            'openat(AT_FDCWD, "test_parent_task.py", O_RDONLY) = 3</fixture/test_parent_task.py>\n'
            f'exit_group({code}) = ?\n')
    assert module().read_process_evidence(tmp_path / "trace", Path("/fixture"), Path("/usr/bin/python"))["tests"] == []


def test_linux_real_process_observer_handles_shell_setup(tmp_path):
    import os
    import shutil
    import subprocess
    import sys
    import pytest
    if sys.platform != "linux" or not shutil.which("strace"):
        pytest.skip("Linux strace observation")
    (tmp_path / "test_parent_task.py").write_text(
        "import unittest\nclass Test(unittest.TestCase):\n def test_ok(self): self.assertEqual(1, 1)\n")
    prefix = tmp_path / "trace"
    command = 'export EXAMPLE=1 && "$CODEXHUB_E2E_PYTHON" -m unittest test_parent_task.py -q'
    result = subprocess.run(
        ["strace", "-ff", "-qq", "-yy", "-s", "8192", "-e", "trace=execve,exit_group,openat",
         "-o", str(prefix), "/bin/sh", "-c", command],
        cwd=tmp_path, env=dict(os.environ, CODEXHUB_E2E_PYTHON=sys.executable), capture_output=True)
    assert result.returncode == 0
    evidence = module().read_process_evidence(prefix, tmp_path, Path(sys.executable))
    assert len(evidence["tests"]) == 1


def _write_test_evidence(path: Path, *, outcome: str = "passed", extra_case: bool = False,
                         changed_source: bool = False) -> None:
    recorder = {"recorder_path": "/harness/e2e_test_recorder.py", "recorder_sha256": "a" * 64}
    events = [
        {
            "schema": "codexhub.e2e-test-evidence.v1", "timestamp": "2026-09-10T00:00:00Z",
            "pid": 77, "event": "suite_started", "expected_cases": ["fixture.Test.test_ok"],
            "source_sha": "a", "test_sha": "b",
            **recorder,
        },
        {
            "schema": "codexhub.e2e-test-evidence.v1", "timestamp": "2026-09-10T00:00:01Z",
            "pid": 77, "event": "case_started", "case": "fixture.Test.test_ok",
            **recorder,
        },
        {
            "schema": "codexhub.e2e-test-evidence.v1", "timestamp": "2026-09-10T00:00:02Z",
            "pid": 77, "event": "case_finished", "case": "fixture.Test.test_ok", "outcome": outcome,
            **recorder,
        },
    ]
    if extra_case:
        events.append({
            "schema": "codexhub.e2e-test-evidence.v1", "timestamp": "2026-09-10T00:00:02Z",
            "pid": 77, "event": "case_finished", "case": "fixture.Test.fake", "outcome": "passed",
            **recorder,
        })
    events.append({
        "schema": "codexhub.e2e-test-evidence.v1", "timestamp": "2026-09-10T00:00:03Z",
        "pid": 77, "event": "suite_finished", "expected_cases": ["fixture.Test.test_ok"],
        "case_outcomes": {"fixture.Test.test_ok": outcome},
        "complete": outcome == "passed" and not extra_case,
        "source_sha": "changed" if changed_source else "a", "test_sha": "b",
        **recorder,
    })
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_test_recorder_requires_complete_fingerprinted_case_set(tmp_path):
    m = module()
    path = tmp_path / "evidence.jsonl"
    _write_test_evidence(path)
    evidence = m.read_test_evidence(path, expected_cases=("fixture.Test.test_ok",))
    assert evidence["complete"] is True
    assert evidence["verified_cases"] == ["fixture.Test.test_ok"]

    _write_test_evidence(path, outcome="skipped")
    assert m.read_test_evidence(path, expected_cases=("fixture.Test.test_ok",))["complete"] is False

    _write_test_evidence(path, extra_case=True)
    rejected = m.read_test_evidence(path, expected_cases=("fixture.Test.test_ok",))
    assert rejected["rejection"] == "test_case_set_unexpected"

    _write_test_evidence(path, changed_source=True)
    rejected = m.read_test_evidence(path, expected_cases=("fixture.Test.test_ok",))
    assert rejected["rejection"] == "test_source_changed_during_execution"


def test_test_recorder_rejects_missing_outcomes_and_nonmonotonic_events(tmp_path):
    m = module()
    path = tmp_path / "evidence.jsonl"
    _write_test_evidence(path)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[-1].pop("case_outcomes")
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    assert m.read_test_evidence(path, expected_cases=("fixture.Test.test_ok",))["rejection"] == (
        "test_suite_case_outcomes_missing_or_extra"
    )

    _write_test_evidence(path)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    events[2]["timestamp"] = "2026-09-09T23:59:59Z"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    assert m.read_test_evidence(path, expected_cases=("fixture.Test.test_ok",))["rejection"] == (
        "test_event_time_order_invalid"
    )


def test_process_evidence_records_fixed_recorder_open(tmp_path):
    m = module()
    trace = tmp_path / "trace.101"
    recorder = Path("/harness/e2e_test_recorder.py")
    trace.write_text(
        'execve("/usr/bin/python", ["python", "-m", "unittest", "test_parent_task.py"], 0x1) = 0\n'
        'openat(AT_FDCWD, "/harness/__pycache__/e2e_test_recorder.cpython-314.pyc", O_RDONLY) = 3</harness/__pycache__/e2e_test_recorder.cpython-314.pyc>\n'
        'openat(AT_FDCWD, "test_parent_task.py", O_RDONLY) = 4</fixture/test_parent_task.py>\n'
        'exit_group(0) = ?\n'
    )
    evidence = m.read_process_evidence(
        tmp_path / "trace", Path("/fixture"), Path("/usr/bin/python"), recorder_path=recorder
    )
    assert evidence["recorder_open_verified"] is True
    assert evidence["processes"][0]["recorder_open_verified"] is True


def test_process_evidence_requires_the_unittest_process_to_write_the_recorder_log(tmp_path):
    m = module()
    trace = tmp_path / "trace.101"
    evidence_path = tmp_path / "test-evidence.jsonl"
    trace.write_text(
        'execve("/usr/bin/python", ["python", "-m", "unittest", "test_parent_task.py"], 0x1) = 0\n'
        f'openat(AT_FDCWD, "{evidence_path}", O_WRONLY|O_CREAT|O_APPEND, 0o600) = 3<{evidence_path}>\n'
        'openat(AT_FDCWD, "test_parent_task.py", O_RDONLY) = 4</fixture/test_parent_task.py>\n'
        'exit_group(0) = ?\n'
    )
    evidence = m.read_process_evidence(
        tmp_path / "trace", Path("/fixture"), Path("/usr/bin/python"), evidence_path=evidence_path
    )
    assert evidence["evidence_write_verified"] is True
    assert evidence["processes"][0]["evidence_write_verified"] is True


def test_process_evidence_correlates_unittest_to_client_tree(tmp_path):
    m = module()
    trace = tmp_path / "trace"
    (tmp_path / "trace.10").write_text(
        'execve("/usr/bin/codex", ["codex", "exec"], 0x1) = 0\n'
        'exit_group(0) = ?\n'
    )
    (tmp_path / "trace.11").write_text(
        'execve("/usr/bin/python", ["python", "-m", "unittest", "test_parent_task.py"], 0x1) = 0\n'
        'openat(AT_FDCWD, "test_parent_task.py", O_RDONLY) = 3</fixture/test_parent_task.py>\n'
        'exit_group(0) = ?\n'
    )
    evidence = m.read_process_evidence(
        trace,
        Path("/fixture"),
        Path("/usr/bin/python"),
        client_entrypoint="codex",
        observed_process_tree={10: {11}},
        observed_client_pids={10},
    )
    assert evidence["client_tree_verified"] is True
    assert evidence["reachable_test_pids"] == [11]

    rejected = m.read_process_evidence(
        trace,
        Path("/fixture"),
        Path("/usr/bin/python"),
        client_entrypoint="codex",
        observed_process_tree={10: {12}},
        observed_client_pids={10},
    )
    assert rejected["client_tree_verified"] is False
