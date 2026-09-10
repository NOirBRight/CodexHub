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
        raw = path.read_text(encoding="utf-8", errors="strict")
        for line in raw.splitlines():
            for name in ("parent_task.py", "test_parent_task.py", "test_resume_turn.py"):
                if ("<" + str(fixture / name) + ">") in line and re.search(r'O_WRONLY|O_RDWR', line):
                    stamp = re.match(r"^(\d+\.\d+) ", line)
                    writes.append({"pid": pid, "file": name,
                                   "timestamp": float(stamp[1]) if stamp else None})
        text = re.sub(r"^\d+\.\d+ ", "", raw, flags=re.M)
        images = list(re.finditer(r'^execve\(("(?:[^"\\]|\\.)*"), (\[.*?\]), .*\)\s+= 0$', text, re.M))
        if not images:
            continue
        try:
            executable, argv = (ast.literal_eval(value) for value in images[-1].groups())
        except (ValueError, SyntaxError):
            continue
        text = text[images[-1].end():]
        opened = set(re.findall(r'= \d+<([^>]+)>', text))
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
