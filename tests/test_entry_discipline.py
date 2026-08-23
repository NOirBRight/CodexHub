"""Pin the Gateway entry after the exec-facade deletion (#466 / #448)."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PYTHON = REPO_ROOT / "src-python"
ENTRY_PATH = SRC_PYTHON / "codex_proxy.py"
ENTRY_LINE_BUDGET = 500
GATEWAY_TOP_LEVEL_LINE_BUDGET = 3000
PACKAGE_LINE_BUDGET = 2000

ENTRY_ALLOWED_FIRST_PARTY = frozenset(
    {
        "python_runtime_contract",
        "gateway_admission",
        "gateway_catalog_runtime",
        "gateway_events",
        "gateway_settings",
        "route_plan",
        "gateway_errors",
        "gateway_handler_impl",
        "gateway_request",
        "protocol_translation",
    }
)

STDLIB_ROOTS = frozenset(
    {
        "__future__",
        "argparse",
        "logging",
        "sys",
        "threading",
        "typing",
        "pathlib",
        "http",
        "urllib",
        "collections",
        "dataclasses",
        "json",
        "os",
        "re",
        "time",
        "uuid",
        "io",
        "functools",
    }
)


def _line_count(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def _first_party_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in STDLIB_ROOTS:
                    names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root not in STDLIB_ROOTS:
                names.add(node.module)
    return names


def test_gateway_runtime_facade_file_is_gone() -> None:
    assert not (SRC_PYTHON / "gateway_runtime.py").exists()
    assert not (SRC_PYTHON / "gateway_runtime").exists()


def test_codex_proxy_entry_has_no_exec_compile_or_handler_setattr() -> None:
    source = ENTRY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"exec", "compile", "setattr"}
    assert "exec(" not in source
    assert "setattr(CodexProxyHandler" not in source
    assert _line_count(ENTRY_PATH) <= ENTRY_LINE_BUDGET


def test_codex_proxy_imports_only_the_real_module_set() -> None:
    tree = ast.parse(ENTRY_PATH.read_text(encoding="utf-8"))
    imported = _first_party_imports(tree)
    assert imported <= ENTRY_ALLOWED_FIRST_PARTY, sorted(imported - ENTRY_ALLOWED_FIRST_PARTY)


def test_gateway_module_files_stay_under_line_budgets() -> None:
    oversized: list[str] = []
    for path in sorted(SRC_PYTHON.glob("gateway_*.py")):
        count = _line_count(path)
        if count >= GATEWAY_TOP_LEVEL_LINE_BUDGET:
            oversized.append(f"{path.name}={count}")
    for package in ("tool_compatibility", "gateway_compat"):
        for path in sorted((SRC_PYTHON / package).glob("*.py")):
            count = _line_count(path)
            if count >= PACKAGE_LINE_BUDGET:
                oversized.append(f"{package}/{path.name}={count}")
    assert oversized == []
