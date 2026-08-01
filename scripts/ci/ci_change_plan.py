"""Fail-closed path planner for the repository CI matrix.

The classifier is deliberately small and deterministic.  It is the only
place that decides whether a formal CI job is applicable to a pull request;
the workflow itself always creates the checks and the final ``CI / gate``
decides whether skipped jobs were legitimately out of scope.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence


ROOT = Path(__file__).resolve().parents[2]

JOB_KEYS = (
    "python_core",
    "python_synthetic",
    "frontend",
    "rust",
    "rust_safe_file_linux",
    "release_flavor",
)

SYNTHETIC_EXACT = frozenset(
    {
        "scripts/run-realcliente2e.ps1",
        "tests/test_real_client_e2e.py",
        "docs/agents/real-client-e2e.md",
        "pytest.ini",
    }
)
SYNTHETIC_TEST_PATH = "tests/test_real_client_e2e.py"
SYNTHETIC_PREFIXES = (
    "tests/fixtures/real_client_e2e/",
)

PYTHON_CORE_EXACT = frozenset(
    {
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "requirements.txt",
        "requirements-dev.txt",
    }
)
PYTHON_CORE_PREFIXES = (
    "src-python/",
    "config/",
)

FRONTEND_EXACT = frozenset(
    {
        "src-tauri/src/app_updates.rs",
        "src-tauri/src/autostart.rs",
        "src-tauri/src/catalog.rs",
        "src-tauri/src/config.rs",
        "src-tauri/src/gateway.rs",
        "src-tauri/src/history.rs",
        "src-tauri/src/models.rs",
        "src-tauri/src/official_refresh.rs",
        "src-tauri/src/openai_usage.rs",
        "src-tauri/src/web_bridge.rs",
        "src-tauri/src/main.rs",
        "src-tauri/capabilities/default.json",
        "src-tauri/cargo.toml",
        "src-tauri/tauri.conf.json",
        "config/build-flavors.json",
        "scripts/build-windows-portable.ps1",
        "scripts/build-windows-release.ps1",
        "scripts/e2e-app-update.ps1",
        "scripts/prepare-pythonruntime.ps1",
        "tests/fixtures/model_identity_vectors.json",
        "design.md",
        "docs/agents/user-feedback.md",
        "frontend/src/lib/tauri.ts",
        "frontend/src/lib/types.ts",
        "frontend/src/lib/ui-contract.json",
    }
)

RELEASE_EXACT = frozenset(
    {
        "config/build-flavors.json",
        "scripts/build-tauriconfig.ps1",
        "scripts/build-windows-portable.ps1",
        "scripts/build-windows-release.ps1",
        "scripts/e2e-app-update.ps1",
        "scripts/new-releasechannelplan.ps1",
        "scripts/prepare-pythonruntime.ps1",
        "scripts/releasechannel.ps1",
        "scripts/test-buildflavorreplacement.ps1",
        "scripts/test-releasemanifest.ps1",
        "scripts/test-windowsautostart.ps1",
        "scripts/test-windowsautostartuninstall.ps1",
        "scripts/run-issue160virtualboxsmoke.ps1",
        "docs/agents/real-client-e2e.md",
        "docs/agents/windows-autostart-smoke.md",
        "tests/test_release_channel_scripts.py",
        "src-tauri/tauri.conf.json",
        "src-tauri/src/app_flavor.rs",
        "src-tauri/src/app_updates.rs",
        "src-tauri/windows/nsis-hooks.nsh",
    }
)
RELEASE_PREFIXES = (
    "src-tauri/icons/",
    "docs/releases/",
)

SAFE_FILE_EXACT = frozenset(
    {
        "src-tauri/src/safe_file.rs",
        "rust-toolchain",
        "rust-toolchain.toml",
        "src-tauri/rust-toolchain",
        "src-tauri/rust-toolchain.toml",
        ".cargo/config",
        ".cargo/config.toml",
    }
)

FULL_EXACT = frozenset(
    {
        ".github/workflows/ci.yml",
        "scripts/ci/ci_change_plan.py",
        "scripts/ci/python_test_plan.py",
        "scripts/ci/check_python_test_partitions.py",
        "tests/test_ci_change_plan.py",
        "tests/test_ci_python_plan.py",
        "docs/agents/ci.md",
        "docs/agents/verification-policy.md",
        "docs/agents/self-hosted-runner.md",
    }
)
FULL_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    "scripts/ci/",
)


@dataclass(frozen=True)
class CIChangePlan:
    """Immutable job-selection result for one event and path set."""

    event_name: str
    full: bool
    python_core: bool
    python_synthetic: bool
    frontend: bool
    rust: bool
    rust_safe_file_linux: bool
    release_flavor: bool
    changed_paths_available: bool
    classifier_failed: bool
    reason: str

    def selected_jobs(self) -> tuple[str, ...]:
        return tuple(key for key in JOB_KEYS if getattr(self, key))

    def to_dict(self) -> dict[str, object]:
        return {
            "event_name": self.event_name,
            "full": self.full,
            "python_core": self.python_core,
            "python_synthetic": self.python_synthetic,
            "frontend": self.frontend,
            "rust": self.rust,
            "rust_safe_file_linux": self.rust_safe_file_linux,
            "release_flavor": self.release_flavor,
            "changed_paths_available": self.changed_paths_available,
            "classifier_failed": self.classifier_failed,
            "selected_jobs": list(self.selected_jobs()),
            "reason": self.reason,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lower()


def _matches(path: str, exact: Iterable[str], prefixes: Iterable[str] = ()) -> bool:
    normalized = _normalize_path(path)
    return normalized in exact or any(normalized.startswith(prefix) for prefix in prefixes)


def _is_python_test(path: str) -> bool:
    normalized = _normalize_path(path)
    if not normalized.startswith("tests/") or not normalized.endswith(".py"):
        return False
    return normalized != SYNTHETIC_TEST_PATH and not normalized.startswith(
        "tests/fixtures/real_client_e2e/"
    )


def _is_python_script(path: str) -> bool:
    normalized = _normalize_path(path)
    return normalized.startswith("scripts/") and normalized.endswith(".py")


def _is_docs_only(path: str) -> bool:
    normalized = _normalize_path(path)
    return normalized.startswith("docs/") or normalized in {
        "readme.md",
        "readme.zh-cn.md",
        "context.md",
        "agents.md",
        "design.md",
        "changelog.md",
        "contributing.md",
        "license",
        "license.md",
        "license.txt",
    }


def _classify_path(path: str) -> tuple[frozenset[str], bool]:
    """Return selected job families and whether the path is recognized."""

    normalized = _normalize_path(path)
    categories: set[str] = set()
    if _matches(path, PYTHON_CORE_EXACT, PYTHON_CORE_PREFIXES) or _is_python_test(path) or _is_python_script(path):
        categories.add("python_core")
    if _matches(path, SYNTHETIC_EXACT, SYNTHETIC_PREFIXES):
        categories.add("python_synthetic")
    if _matches(path, FRONTEND_EXACT, ("frontend/",)):
        categories.add("frontend")
    if (
        normalized.startswith("src-tauri/")
        or normalized in {"cargo.toml", "cargo.lock"}
        or normalized.endswith("/cargo.toml")
        or normalized.endswith("/cargo.lock")
    ):
        categories.add("rust")
    if _matches(path, SAFE_FILE_EXACT) or normalized.endswith("/safe_file.rs"):
        categories.add("rust_safe_file_linux")
    if _matches(path, RELEASE_EXACT, RELEASE_PREFIXES) or normalized.startswith("scripts/build-windows-"):
        categories.add("release_flavor")

    recognized = bool(categories) or _is_docs_only(path)
    return frozenset(categories), recognized


def _all_jobs(*, event_name: str, reason: str, classifier_failed: bool = False) -> CIChangePlan:
    return CIChangePlan(
        event_name=event_name,
        full=True,
        python_core=True,
        python_synthetic=True,
        frontend=True,
        rust=True,
        rust_safe_file_linux=True,
        release_flavor=True,
        changed_paths_available=not classifier_failed,
        classifier_failed=classifier_failed,
        reason=reason,
    )


def build_plan(
    event_name: str,
    is_pull_request: bool,
    changed_paths: Optional[Sequence[str]],
) -> CIChangePlan:
    """Classify a path set, failing closed for unavailable or unsafe input.

    ``None`` means that changed-path acquisition failed.  It selects the full
    matrix and marks the classifier failed so the gate can require a repair.
    Non-PR events are intentionally full validation events.
    """

    if not is_pull_request:
        return _all_jobs(
            event_name=event_name,
            reason=f"{event_name} is not a pull request; full validation is required.",
        )
    if changed_paths is None:
        return _all_jobs(
            event_name=event_name,
            reason="Changed-path acquisition failed or was unavailable; full validation is required.",
            classifier_failed=True,
        )

    normalized_paths = tuple(_normalize_path(path) for path in changed_paths if path.strip())
    if not normalized_paths:
        return _all_jobs(
            event_name=event_name,
            reason="A pull request returned no changed paths; full validation is required.",
            classifier_failed=True,
        )

    if any(
        _matches(path, FULL_EXACT, FULL_PREFIXES)
        for path in normalized_paths
    ):
        return _all_jobs(
            event_name=event_name,
            reason="The planner, workflow, or CI contract changed; full validation is required.",
        )

    classified = tuple((path, *_classify_path(path)) for path in normalized_paths)
    unknown = [path for path, _categories, recognized in classified if not recognized]
    if unknown:
        return _all_jobs(
            event_name=event_name,
            reason="Unrecognized path(s) require fail-closed full validation: "
            + ", ".join(unknown),
        )

    selected = frozenset().union(*(categories for _path, categories, _recognized in classified))
    return CIChangePlan(
        event_name=event_name,
        full=False,
        python_core="python_core" in selected,
        python_synthetic="python_synthetic" in selected,
        frontend="frontend" in selected,
        rust="rust" in selected,
        rust_safe_file_linux="rust_safe_file_linux" in selected,
        release_flavor="release_flavor" in selected,
        changed_paths_available=True,
        classifier_failed=False,
        reason="Selected jobs from the immutable changed-path classification.",
    )


def _read_changed_paths_file(path: Path) -> Optional[list[str]]:
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return None


def _changed_paths_from_git(base_sha: str, head_sha: str) -> Optional[list[str]]:
    try:
        subprocess.run(
            ["git", "fetch", "--no-tags", "origin", base_sha],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        merge_base = subprocess.run(
            ["git", "merge-base", base_sha, head_sha],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", merge_base, head_sha],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.CalledProcessError):
        return None


def _plan_from_environment() -> CIChangePlan:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "unknown")
    is_pull_request = event_name == "pull_request"
    if not is_pull_request:
        return build_plan(event_name, False, [])

    base_sha = os.environ.get("CI_PR_BASE_SHA", "")
    head_sha = os.environ.get("CI_PR_HEAD_SHA", "")
    if not base_sha or not head_sha:
        return build_plan(event_name, True, None)
    return build_plan(event_name, True, _changed_paths_from_git(base_sha, head_sha))


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan CodexHub CI jobs from changed paths.")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "unknown"))
    parser.add_argument("--is-pull-request", action="store_true")
    parser.add_argument("--changed-paths", nargs="*", default=None)
    parser.add_argument("--changed-paths-file", default=None)
    parser.add_argument("--output-json", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when changed-path acquisition failed.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    is_pull_request = args.is_pull_request or args.event == "pull_request"
    if args.changed_paths is not None:
        changed_paths: Optional[Sequence[str]] = args.changed_paths
    elif args.changed_paths_file:
        changed_paths = _read_changed_paths_file(Path(args.changed_paths_file))
    elif is_pull_request:
        changed_paths = _changed_paths_from_git(
            os.environ.get("CI_PR_BASE_SHA", ""),
            os.environ.get("CI_PR_HEAD_SHA", ""),
        )
    else:
        changed_paths = []

    plan = build_plan(args.event, is_pull_request, changed_paths)
    if args.output_json:
        print(plan.to_json())
    else:
        print(plan.reason)
        print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    return 1 if args.strict and plan.classifier_failed else 0


if __name__ == "__main__":
    sys.exit(main())
