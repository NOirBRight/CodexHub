from pathlib import Path
import importlib.util


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
    m = module()
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
