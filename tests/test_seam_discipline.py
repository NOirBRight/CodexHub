"""Guard the Gateway test surface against private ``codex_proxy`` internals."""

from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = re.compile(
    r"codex_proxy\._[A-Za-z]|CodexProxyHandler\.__new__|CodexProxyHandler\._"
)

TESTS_ROOT = Path(__file__).resolve().parent


def test_gateway_tests_do_not_touch_codex_proxy_privates() -> None:
    hits: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(TESTS_ROOT)}:{line_no}:{line.strip()}")
    assert hits == []
