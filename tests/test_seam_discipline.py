"""Guard the Gateway test surface against private ``codex_proxy`` internals."""

from __future__ import annotations

import re
from pathlib import Path

FORBIDDEN = re.compile(
    r"codex_proxy\._[A-Za-z]|CodexProxyHandler\.__new__|CodexProxyHandler\._"
)

# After T5 the Gateway entry is not a patch surface (#466).
FORBIDDEN_ENTRY_PATCH = re.compile(
    r"""(?:patch\(\s*['\"]codex_proxy\.|monkeypatch\.setattr\(\s*codex_proxy\b|setattr\(\s*codex_proxy\b)"""
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


def test_gateway_tests_do_not_patch_the_entry_module() -> None:
    hits: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        collapsed = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        if FORBIDDEN_ENTRY_PATCH.search(collapsed):
            hits.append(str(path.relative_to(TESTS_ROOT)))
    assert hits == []
