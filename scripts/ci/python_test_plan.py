"""Deterministic Python CI test planner for CodexHub.

This seam decides which Python tests run for a given GitHub Actions event and
changed-path set. It is intentionally free of third-party path-filter actions
and relies only on git and pytest semantics.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parents[2]

REAL_CLIENT_E2E = "tests/test_real_client_e2e.py"

# Paths that may affect the synthetic real-client E2E contract. Directory entries
# must end with a slash so any nested file under that directory is relevant.
RELEVANT_SYNTHETIC_PATHS = [
    "scripts/Run-RealClientE2E.ps1",
    REAL_CLIENT_E2E,
    "tests/fixtures/real_client_e2e/",
    "docs/agents/real-client-e2e.md",
    ".github/workflows/ci.yml",
    "scripts/ci/python_test_plan.py",
    "scripts/ci/check_python_test_partitions.py",
    "tests/test_ci_python_plan.py",
    "pytest.ini",
]


@dataclass(frozen=True)
class PythonTestPlan:
    """Immutable plan for one CI partition decision."""

    event_name: str
    core_args: List[str]
    synthetic_status: str  # "run" or "not_applicable"
    synthetic_args: Optional[List[str]]
    description: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _normalize_path(path: str) -> str:
    """Return a forward-slash path relative to the repo root."""
    return path.replace("\\", "/").lstrip("/")


def is_relevant_synthetic_path(path: str) -> bool:
    """Return True if a changed path should trigger the synthetic suite on a PR."""
    normalized = _normalize_path(path)
    for relevant in RELEVANT_SYNTHETIC_PATHS:
        if relevant.endswith("/"):
            prefix = relevant
            exact = relevant.rstrip("/")
            if normalized.startswith(prefix) or normalized == exact:
                return True
        else:
            if normalized == relevant:
                return True
    return False


def has_relevant_synthetic_path(changed_paths: List[str]) -> bool:
    return any(is_relevant_synthetic_path(p) for p in changed_paths)


def _core_args() -> List[str]:
    return [
        "-q",
        f"--ignore={REAL_CLIENT_E2E}",
        "--junitxml=.pytest-results/junit-core.xml",
        "--durations=0",
    ]


def _synthetic_args() -> List[str]:
    return [
        "-q",
        REAL_CLIENT_E2E,
        "--junitxml=.pytest-results/junit-synthetic.xml",
        "--durations=0",
    ]


def build_plan(
    event_name: str,
    is_pull_request: bool,
    changed_paths: Optional[List[str]],
) -> PythonTestPlan:
    """Build a deterministic test plan from event metadata.

    PR events run the synthetic suite only when relevant paths changed.
    A ``None`` changed-path set means acquisition failed or was unavailable,
    so PRs fail closed and run the synthetic suite. Every other event type
    also fails closed and always runs the synthetic suite. The core partition
    always runs on PRs.
    """
    core_args = _core_args()

    if is_pull_request:
        if changed_paths is None:
            synthetic_status = "run"
            synthetic_args = _synthetic_args()
            description = (
                "Pull request changed-path acquisition failed or was "
                "unavailable; fail closed by running the synthetic "
                "real-client contract."
            )
        elif has_relevant_synthetic_path(changed_paths):
            synthetic_status = "run"
            synthetic_args = _synthetic_args()
            description = (
                "Pull request touched a real-client E2E dependency; "
                "running the synthetic real-client contract."
            )
        else:
            synthetic_status = "not_applicable"
            synthetic_args = None
            description = (
                "Pull request did not touch a real-client E2E dependency; "
                "synthetic real-client contract is not applicable."
            )
    else:
        synthetic_status = "run"
        synthetic_args = _synthetic_args()
        description = (
            f"{event_name} is not a pull request; fail closed by running "
            "the synthetic real-client contract."
        )

    return PythonTestPlan(
        event_name=event_name,
        core_args=core_args,
        synthetic_status=synthetic_status,
        synthetic_args=synthetic_args,
        description=description,
    )


def _read_changed_paths_file(path: Path) -> Optional[List[str]]:
    """Read changed paths or return ``None`` when the file is missing/unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return [line.strip() for line in text.splitlines() if line.strip()]


def _changed_paths_from_git(base_ref: str, head_ref: str) -> Optional[List[str]]:
    """Return changed paths between the merge-base and head, or ``None`` on failure.

    Uses ``git merge-base`` so the comparison is against the actual PR fork
    point, not a naive base-tip diff. Any fetch, merge-base, or diff failure
    returns ``None`` so the caller can fail closed.
    """
    try:
        subprocess.run(
            ["git", "fetch", "origin", base_ref, "--depth=1"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        merge_base = subprocess.run(
            ["git", "merge-base", f"origin/{base_ref}", head_ref],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result = subprocess.run(
            ["git", "diff", "--name-only", merge_base, head_ref],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        return None


def _plan_from_environment() -> PythonTestPlan:
    """Build a plan using GitHub Actions environment variables.

    Expects GITHUB_EVENT_NAME. For pull_request events, uses GITHUB_BASE_REF and
    GITHUB_HEAD_REF to compute changed paths.
    """
    event_name = os.environ.get("GITHUB_EVENT_NAME", "unknown")
    is_pull_request = event_name == "pull_request"

    if is_pull_request:
        base_ref = os.environ.get("GITHUB_BASE_REF", "")
        head_ref = os.environ.get("GITHUB_HEAD_REF", "HEAD")
        if base_ref:
            changed_paths = _changed_paths_from_git(base_ref, head_ref)
        else:
            # Missing base ref means path acquisition is unavailable; fail closed.
            changed_paths = None
    else:
        changed_paths = []

    return build_plan(event_name, is_pull_request, changed_paths)


def main_plan_from_environment() -> PythonTestPlan:
    """Public entry point used by tests and CI for environment-based planning."""
    return _plan_from_environment()


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan Python test partitions for CodexHub CI.",
    )
    parser.add_argument(
        "--event",
        default=os.environ.get("GITHUB_EVENT_NAME", "unknown"),
        help="GitHub Actions event name (default: GITHUB_EVENT_NAME).",
    )
    parser.add_argument(
        "--is-pull-request",
        action="store_true",
        help="Treat the event as a pull request.",
    )
    parser.add_argument(
        "--changed-paths",
        nargs="*",
        default=None,
        help="Explicit list of changed paths for a pull request.",
    )
    parser.add_argument(
        "--changed-paths-file",
        default=None,
        help="UTF-8 file with one changed path per line for a pull request.",
    )
    parser.add_argument(
        "--output-json",
        action="store_true",
        help="Emit the plan as JSON instead of human-readable text.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    event_name = args.event
    is_pull_request = args.is_pull_request or event_name == "pull_request"

    if args.changed_paths is not None:
        changed_paths = args.changed_paths
    elif args.changed_paths_file:
        changed_paths = _read_changed_paths_file(Path(args.changed_paths_file))
    elif is_pull_request:
        changed_paths = _changed_paths_from_git(
            os.environ.get("GITHUB_BASE_REF", "dev"),
            os.environ.get("GITHUB_HEAD_REF", "HEAD"),
        )
    else:
        changed_paths = []

    plan = build_plan(event_name, is_pull_request, changed_paths)

    if args.output_json:
        print(plan.to_json())
    else:
        print(plan.description)
        print(f"Core args: {' '.join(plan.core_args)}")
        if plan.synthetic_status == "run":
            print(f"Synthetic args: {' '.join(plan.synthetic_args)}")
        else:
            print("Synthetic: not applicable")

    return 0


if __name__ == "__main__":
    sys.exit(main())
