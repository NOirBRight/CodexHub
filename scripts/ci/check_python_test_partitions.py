"""Verify Python test partition completeness without executing tests.

The CI planner splits Python tests into a "core" partition (everything except
``tests/test_real_client_e2e.py``) and a "synthetic" partition
(``tests/test_real_client_e2e.py``). This checker proves, by collection only,
that the two partitions are disjoint and their union equals the full collection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from python_runtime_contract import require_python_313

require_python_313(__file__)


ROOT = Path(__file__).resolve().parents[2]
REAL_CLIENT_E2E = "tests/test_real_client_e2e.py"


def _collect(nodeids: List[str]) -> Set[str]:
    """Collect pytest nodeids for the given test selection.

    Always uses ``--collect-only`` so no test code executes.
    """
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
    ] + nodeids
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src-python")
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    collected: Set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("no tests"):
            continue
        if "test session starts" in line.lower():
            continue
        if "collected" in line.lower():
            continue
        # The whole line is the nodeid; pytest -q emits one nodeid per line.
        collected.add(line)
    return collected


def _partition_report(
    full: Set[str],
    core: Set[str],
    synthetic: Set[str],
) -> dict:
    overlap = core & synthetic
    union = core | synthetic
    missing = full - union
    extra = union - full

    return {
        "full_count": len(full),
        "core_count": len(core),
        "synthetic_count": len(synthetic),
        "overlap_count": len(overlap),
        "overlap": sorted(overlap),
        "missing_count": len(missing),
        "missing": sorted(missing),
        "extra_count": len(extra),
        "extra": sorted(extra),
        "disjoint": len(overlap) == 0,
        "complete": full == union,
    }


def main() -> int:
    print("Collecting full Python test set...")
    full = _collect([])
    print(f"  full: {len(full)} tests")

    print("Collecting core partition (excluding real-client E2E)...")
    core = _collect(["--ignore", REAL_CLIENT_E2E])
    print(f"  core: {len(core)} tests")

    print("Collecting synthetic partition (real-client E2E only)...")
    synthetic = _collect([REAL_CLIENT_E2E])
    print(f"  synthetic: {len(synthetic)} tests")

    report = _partition_report(full, core, synthetic)

    print()
    print(f"Disjoint: {report['disjoint']} (overlap={report['overlap_count']})")
    print(f"Union complete: {report['complete']} (missing={report['missing_count']}, extra={report['extra_count']})")

    if report["overlap"]:
        print("Overlap nodeids:")
        for nodeid in report["overlap"]:
            print(f"  {nodeid}")
    if report["missing"]:
        print("Missing from union:")
        for nodeid in report["missing"]:
            print(f"  {nodeid}")
    if report["extra"]:
        print("Extra in union:")
        for nodeid in report["extra"]:
            print(f"  {nodeid}")

    return 0 if report["disjoint"] and report["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
