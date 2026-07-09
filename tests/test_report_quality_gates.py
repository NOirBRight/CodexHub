import json
import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "report_quality_gates.py"


def run_report(root: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--json"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_report_quality_gates_finds_python_unused_imports_and_dead_functions(tmp_path):
    source = tmp_path / "src-python" / "sample.py"
    source.parent.mkdir()
    source.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations
            import json
            import os

            def used_helper():
                return json.dumps({"ok": True})

            def dead_helper():
                return False

            used_helper()
            """
        ),
        encoding="utf-8",
    )

    report = run_report(tmp_path)

    assert report["summary"]["mode"] == "report-only"
    assert {
        "path": "src-python/sample.py",
        "name": "os",
        "line": 4,
        "reason": "imported name is never referenced",
    } in report["python_unused_imports"]
    assert all(item["name"] != "annotations" for item in report["python_unused_imports"])
    assert {
        "path": "src-python/sample.py",
        "name": "dead_helper",
        "line": 9,
        "reason": "top-level function is never referenced in scanned Python names",
    } in report["python_dead_functions"]


def test_report_quality_gates_reports_duplicate_helper_names_and_honors_allowlist(tmp_path):
    python_source = tmp_path / "src-python" / "helpers.py"
    ts_source = tmp_path / "frontend" / "src" / "helpers.ts"
    rust_source = tmp_path / "src-tauri" / "src" / "helpers.rs"
    config = tmp_path / "config" / "report-quality-allowlist.json"
    python_source.parent.mkdir()
    ts_source.parent.mkdir(parents=True)
    rust_source.parent.mkdir(parents=True)
    config.parent.mkdir()

    python_source.write_text("def duplicated_helper():\n    return True\n", encoding="utf-8")
    ts_source.write_text("function duplicated_helper() { return true; }\n", encoding="utf-8")
    rust_source.write_text("fn legacy_compat_name() {}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "legacy.ts").write_text(
        "function legacy_compat_name() { return true; }\n",
        encoding="utf-8",
    )
    config.write_text(
        json.dumps(
            {
                "duplicate_function_names": [
                    {
                        "name": "legacy_compat_name",
                        "reason": "intentional legacy compatibility shim",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = run_report(tmp_path)

    duplicate_names = {item["name"] for item in report["duplicate_function_names"]}
    assert "duplicated_helper" in duplicate_names
    assert "legacy_compat_name" not in duplicate_names
    duplicated = next(item for item in report["duplicate_function_names"] if item["name"] == "duplicated_helper")
    assert {entry["language"] for entry in duplicated["locations"]} == {"python", "typescript"}
