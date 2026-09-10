"""Linux kernel-observed fixture execution; raw traces stay in private temp storage."""
from __future__ import annotations

from python_runtime_contract import require_python_313
require_python_313(__file__)

import ast
from pathlib import Path
import re


def read_process_evidence(prefix: Path, fixture: Path, interpreter: Path) -> dict:
    tests = []
    writes = []
    files = list(prefix.parent.glob(prefix.name + ".*"))
    for path in files:
        if not path.suffix[1:].isdigit():
            continue
        pid = int(path.suffix[1:])
        text = path.read_text(encoding="utf-8", errors="strict")
        # Only the final successful exec image can own the process exit.
        images = re.findall(r'^execve\(("(?:[^"\\]|\\.)*"), (\[.*?\]), .*\)\s+= 0$', text, re.M)
        if not images:
            continue
        try:
            executable, argv = (ast.literal_eval(value) for value in images[-1])
        except (ValueError, SyntaxError):
            continue
        opened = set(re.findall(r'= \d+<([^>]+)>', text))
        for name in ("parent_task.py", "test_parent_task.py", "test_resume_turn.py"):
            target = str(fixture / name)
            if any(target in line and re.search(r'O_WRONLY|O_RDWR', line)
                   for line in text.splitlines()):
                writes.append({"pid": pid, "file": name})
        if Path(executable).resolve() != interpreter.resolve():
            continue
        if not isinstance(argv, list) or argv[1:3] != ["-m", "unittest"]:
            continue
        if not re.search(r'^exit_group\(0\)', text, re.M):
            continue
        for name in ("test_parent_task.py", "test_resume_turn.py"):
            # A real interpreter must open this run's fixture test, not merely
            # print its name or run another directory's identically named test.
            if str(fixture / name) in opened:
                tests.append({"pid": pid, "test": name, "exit_code": 0,
                              "interpreter_verified": True, "fixture_open_verified": True})
    return {"available": bool(files), "tests": tests, "writes": writes}
