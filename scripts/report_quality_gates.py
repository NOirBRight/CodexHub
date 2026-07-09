#!/usr/bin/env python3
"""Report-only quality gates for dead code and duplicated helper names.

This script intentionally exits 0 for findings. It is a visibility tool, not a
blocking gate. Review the output, then promote individual checks later once the
noise is understood.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCAN_DIRS = ("src-python", "scripts", "frontend/src", "src-tauri/src")
ALLOWLIST_PATH = Path("config/report-quality-allowlist.json")


@dataclass(frozen=True)
class FunctionLocation:
    name: str
    path: str
    line: int
    language: str


class PythonNameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.loaded_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802 - ast API
        if isinstance(node.ctx, ast.Load):
            self.loaded_names.add(node.id)


def relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def load_allowlist(root: Path) -> dict[str, list[dict[str, Any]]]:
    path = root / ALLOWLIST_PATH
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def is_allowlisted(
    allowlist: dict[str, list[dict[str, Any]]],
    section: str,
    *,
    name: str,
    path: str | None = None,
) -> bool:
    for item in allowlist.get(section, []):
        if item.get("name") != name:
            continue
        if path is not None and item.get("path") not in {None, path}:
            continue
        return True
    return False


def source_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for directory in SCAN_DIRS:
        base = root / directory
        if not base.exists():
            continue
        for suffix in suffixes:
            files.extend(
                path
                for path in base.rglob(f"*{suffix}")
                if "node_modules" not in path.parts and "target" not in path.parts and "dist" not in path.parts
            )
    return sorted(set(files))


def scan_python(root: Path, allowlist: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[FunctionLocation], list[dict[str, Any]]]:
    unused_imports: list[dict[str, Any]] = []
    dead_functions: list[dict[str, Any]] = []
    functions: list[FunctionLocation] = []
    parse_errors: list[dict[str, Any]] = []

    for path in source_files(root, (".py",)):
        rel = relative_path(path, root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            parse_errors.append({"path": rel, "line": exc.lineno, "message": str(exc)})
            continue

        collector = PythonNameCollector()
        collector.visit(tree)
        top_level_functions = [
            node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        functions.extend(
            FunctionLocation(node.name, rel, node.lineno, "python")
            for node in top_level_functions
        )

        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported = alias.asname or alias.name.split(".", 1)[0]
                    if imported not in collector.loaded_names and not is_allowlisted(
                        allowlist,
                        "python_unused_imports",
                        name=imported,
                        path=rel,
                    ):
                        unused_imports.append(
                            {
                                "path": rel,
                                "name": imported,
                                "line": node.lineno,
                                "reason": "imported name is never referenced",
                            }
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    imported = alias.asname or alias.name
                    if imported == "*":
                        continue
                    if imported not in collector.loaded_names and not is_allowlisted(
                        allowlist,
                        "python_unused_imports",
                        name=imported,
                        path=rel,
                    ):
                        unused_imports.append(
                            {
                                "path": rel,
                                "name": imported,
                                "line": node.lineno,
                                "reason": "imported name is never referenced",
                            }
                        )

        for node in top_level_functions:
            if node.name.startswith("__"):
                continue
            if node.name not in collector.loaded_names and not is_allowlisted(
                allowlist,
                "python_dead_functions",
                name=node.name,
                path=rel,
            ):
                dead_functions.append(
                    {
                        "path": rel,
                        "name": node.name,
                        "line": node.lineno,
                        "reason": "top-level function is never referenced in scanned Python names",
                    }
                )

    return unused_imports, dead_functions, functions, parse_errors


TS_FUNCTION_PATTERNS = (
    re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"\bconst\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"),
)
RUST_FUNCTION_PATTERN = re.compile(r"\b(?:pub(?:\([^)]*\))?\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def scan_regex_functions(root: Path) -> list[FunctionLocation]:
    functions: list[FunctionLocation] = []
    for path in source_files(root, (".ts", ".tsx")):
        rel = relative_path(path, root)
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for index, line in enumerate(lines, start=1):
            for pattern in TS_FUNCTION_PATTERNS:
                for match in pattern.finditer(line):
                    functions.append(FunctionLocation(match.group(1), rel, index, "typescript"))
    for path in source_files(root, (".rs",)):
        rel = relative_path(path, root)
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for index, line in enumerate(lines, start=1):
            for match in RUST_FUNCTION_PATTERN.finditer(line):
                functions.append(FunctionLocation(match.group(1), rel, index, "rust"))
    return functions


def duplicate_function_names(
    functions: list[FunctionLocation],
    allowlist: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[FunctionLocation]] = defaultdict(list)
    for function in functions:
        grouped[function.name].append(function)

    duplicates: list[dict[str, Any]] = []
    for name, locations in sorted(grouped.items()):
        distinct_files = {location.path for location in locations}
        if len(distinct_files) < 2:
            continue
        if is_allowlisted(allowlist, "duplicate_function_names", name=name):
            continue
        duplicates.append(
            {
                "name": name,
                "locations": [
                    {
                        "path": location.path,
                        "line": location.line,
                        "language": location.language,
                    }
                    for location in locations
                ],
            }
        )
    return duplicates


def build_report(root: Path) -> dict[str, Any]:
    allowlist = load_allowlist(root)
    unused_imports, dead_functions, python_functions, parse_errors = scan_python(root, allowlist)
    duplicate_functions = duplicate_function_names(
        [*python_functions, *scan_regex_functions(root)],
        allowlist,
    )
    return {
        "summary": {
            "mode": "report-only",
            "allowlist": str(ALLOWLIST_PATH).replace("\\", "/"),
            "python_unused_imports": len(unused_imports),
            "python_dead_functions": len(dead_functions),
            "duplicate_function_names": len(duplicate_functions),
            "parse_errors": len(parse_errors),
        },
        "python_unused_imports": unused_imports,
        "python_dead_functions": dead_functions,
        "duplicate_function_names": duplicate_functions,
        "parse_errors": parse_errors,
    }


def print_text_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("CodexHub report-only quality gates")
    print(f"Mode: {summary['mode']} (exit code remains 0 for findings)")
    print(f"Allowlist: {summary['allowlist']}")
    for section in (
        "python_unused_imports",
        "python_dead_functions",
        "duplicate_function_names",
        "parse_errors",
    ):
        print(f"\n{section}: {len(report[section])}")
        for item in report[section][:50]:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
        if len(report[section]) > 50:
            print(f"... {len(report[section]) - 50} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root to scan")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    report = build_report(args.root.resolve())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
