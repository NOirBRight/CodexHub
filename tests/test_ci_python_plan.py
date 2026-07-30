import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLANNER_PATH = ROOT / "scripts" / "ci" / "python_test_plan.py"
CHECKER_PATH = ROOT / "scripts" / "ci" / "check_python_test_partitions.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plan():
    return _load_module(PLANNER_PATH, "python_test_plan")


def _workflow_job_text(workflow_text: str, job_name: str) -> str:
    """Return the raw text of a top-level GitHub Actions job."""
    start = workflow_text.find(f"  {job_name}:")
    assert start != -1, f"job {job_name!r} not found"
    after = workflow_text[start + 1 :]
    next_job = re.search(r"\n  [a-zA-Z0-9_-]+:", after)
    if next_job:
        end = start + 1 + next_job.start()
    else:
        end = len(workflow_text)
    return workflow_text[start:end]


def _step_run_text(job_text: str, step_name: str) -> str:
    """Return the run-script text for a named step in a job."""
    step_start = job_text.find(f"- name: {step_name}")
    assert step_start != -1, f"step {step_name!r} not found"
    after = job_text[step_start + 1 :]
    next_step = re.search(r"\n  - name:", after)
    if next_step:
        step_end = step_start + 1 + next_step.start()
    else:
        step_end = len(job_text)
    step_text = job_text[step_start:step_end]
    run_match = re.search(r"\n        run: (.*)", step_text)
    if not run_match:
        return ""
    run_first = run_match.group(1).strip()
    if run_first and run_first not in ("|", ">", "|-", ">-"):
        return run_first
    run_lines = []
    for line in step_text[run_match.end() :].splitlines():
        if line.startswith("          "):
            run_lines.append(line[10:])
        elif line == "":
            run_lines.append("")
        else:
            break
    return "\n".join(run_lines)


def test_module_paths_exist():
    assert PLANNER_PATH.exists()
    assert CHECKER_PATH.exists()


def test_core_args_always_ignore_real_client_e2e(plan):
    p = plan.build_plan("pull_request", True, ["src-python/codex_proxy.py"])
    assert "--ignore=tests/test_real_client_e2e.py" in p.core_args


def test_pr_unrelated_paths_synthetic_is_not_applicable(plan):
    p = plan.build_plan("pull_request", True, ["src-python/codex_proxy.py"])
    assert p.synthetic_status == "not_applicable"
    assert p.synthetic_args is None


def test_pr_ci_yml_change_runs_synthetic(plan):
    p = plan.build_plan("pull_request", True, [".github/workflows/ci.yml"])
    assert p.synthetic_status == "run"
    assert p.synthetic_args is not None
    assert "tests/test_real_client_e2e.py" in p.synthetic_args


def test_pr_real_client_e2e_change_runs_synthetic(plan):
    p = plan.build_plan("pull_request", True, ["tests/test_real_client_e2e.py"])
    assert p.synthetic_status == "run"


def test_pr_run_real_client_e2e_script_change_runs_synthetic(plan):
    p = plan.build_plan("pull_request", True, ["scripts/Run-RealClientE2E.ps1"])
    assert p.synthetic_status == "run"


def test_pr_fixture_change_runs_synthetic(plan):
    p = plan.build_plan(
        "pull_request",
        True,
        ["tests/fixtures/real_client_e2e/fake-client-real-contract.cmd"],
    )
    assert p.synthetic_status == "run"


def test_pr_real_client_e2e_doc_change_runs_synthetic(plan):
    p = plan.build_plan("pull_request", True, ["docs/agents/real-client-e2e.md"])
    assert p.synthetic_status == "run"


def test_pr_planner_change_runs_synthetic(plan):
    p = plan.build_plan("pull_request", True, ["scripts/ci/python_test_plan.py"])
    assert p.synthetic_status == "run"


def test_pr_checker_change_runs_synthetic(plan):
    p = plan.build_plan(
        "pull_request", True, ["scripts/ci/check_python_test_partitions.py"]
    )
    assert p.synthetic_status == "run"


def test_pr_planner_test_change_runs_synthetic(plan):
    p = plan.build_plan("pull_request", True, ["tests/test_ci_python_plan.py"])
    assert p.synthetic_status == "run"


def test_pr_pytest_ini_change_runs_synthetic(plan):
    p = plan.build_plan("pull_request", True, ["pytest.ini"])
    assert p.synthetic_status == "run"


def test_pr_mixed_unrelated_and_relevant_runs_synthetic(plan):
    p = plan.build_plan(
        "pull_request",
        True,
        ["src-python/codex_proxy.py", "tests/test_real_client_e2e.py"],
    )
    assert p.synthetic_status == "run"


def test_push_event_fails_closed_and_runs_synthetic(plan):
    p = plan.build_plan("push", False, ["src-python/codex_proxy.py"])
    assert p.synthetic_status == "run"
    assert p.synthetic_args is not None
    assert "tests/test_real_client_e2e.py" in p.synthetic_args


def test_workflow_dispatch_event_fails_closed(plan):
    p = plan.build_plan("workflow_dispatch", False, [])
    assert p.synthetic_status == "run"


def test_schedule_event_fails_closed(plan):
    p = plan.build_plan("schedule", False, [])
    assert p.synthetic_status == "run"


def test_unknown_non_pr_event_fails_closed(plan):
    p = plan.build_plan("release", False, [])
    assert p.synthetic_status == "run"


def test_core_and_synthetic_emit_junit_and_durations(plan):
    p = plan.build_plan("pull_request", True, ["tests/test_real_client_e2e.py"])
    assert "--junitxml=.pytest-results/junit-core.xml" in p.core_args
    assert "--durations=0" in p.core_args
    assert "--junitxml=.pytest-results/junit-synthetic.xml" in p.synthetic_args
    assert "--durations=0" in p.synthetic_args


def test_synthetic_args_do_not_start_run_real_client_e2e_ps1(plan):
    p = plan.build_plan("pull_request", True, ["tests/test_real_client_e2e.py"])
    joined = " ".join(p.synthetic_args)
    assert "Run-RealClientE2E.ps1" not in joined


def test_not_applicable_plan_description_is_explicit(plan):
    p = plan.build_plan("pull_request", True, ["src-python/codex_proxy.py"])
    assert "not applicable" in p.description.lower()


def test_pr_missing_changed_paths_fails_closed_to_synthetic(plan):
    p = plan.build_plan("pull_request", True, None)
    assert p.synthetic_status == "run"
    assert p.synthetic_args is not None


def test_unreadable_changed_paths_file_fails_closed_to_synthetic(tmp_path):
    # A directory at the path makes read_text fail, so the planner treats it as
    # missing/unreadable and fails closed to full validation.
    bad_path = tmp_path / "changed-paths.txt"
    bad_path.mkdir()
    out = subprocess.run(
        [
            sys.executable,
            str(PLANNER_PATH),
            "--event",
            "pull_request",
            "--is-pull-request",
            "--changed-paths-file",
            str(bad_path),
            "--output-json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(out.stdout)
    assert payload["synthetic_status"] == "run"


def test_cli_json_output_matches_build_plan(plan, tmp_path):
    out = subprocess.run(
        [
            sys.executable,
            str(PLANNER_PATH),
            "--event",
            "pull_request",
            "--is-pull-request",
            "--changed-paths",
            "tests/test_real_client_e2e.py",
            "--output-json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(out.stdout)
    assert payload["synthetic_status"] == "run"
    assert "tests/test_real_client_e2e.py" in payload["synthetic_args"]


def test_cli_not_applicable_output_is_valid_json(plan):
    out = subprocess.run(
        [
            sys.executable,
            str(PLANNER_PATH),
            "--event",
            "pull_request",
            "--is-pull-request",
            "--changed-paths",
            "src-python/codex_proxy.py",
            "--output-json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(out.stdout)
    assert payload["synthetic_status"] == "not_applicable"
    assert payload["synthetic_args"] is None


def test_environment_event_name_is_respected(monkeypatch):
    # Reload the module with a patched environment to exercise the CLI default path.
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    fresh = _load_module(PLANNER_PATH, "python_test_plan_env")
    plan = fresh.main_plan_from_environment()
    assert plan.synthetic_status == "run"


def test_checker_runs_without_executing_tests():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src-python")
    out = subprocess.run(
        [sys.executable, str(CHECKER_PATH)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    assert "disjoint" in out.stdout.lower()
    assert "union" in out.stdout.lower()
    assert "true" in out.stdout.lower()


def test_ci_yaml_has_full_checkout_for_synthetic_merge_base():
    """Regression: shallow checkout breaks git merge-base on a fresh PR runner."""
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    synthetic_job = _workflow_job_text(workflow_text, "python-synthetic")
    checkout_start = synthetic_job.find("- name: Check out repository")
    assert checkout_start != -1
    checkout_end = synthetic_job.find("- name:", checkout_start + 1)
    if checkout_end == -1:
        checkout_end = len(synthetic_job)
    checkout_step = synthetic_job[checkout_start:checkout_end]
    assert checkout_step.strip().startswith("- name: Check out repository")
    assert "actions/checkout" in checkout_step
    assert "fetch-depth: 0" in checkout_step


def test_ci_yaml_synthetic_run_uses_watchdog_with_3600s_bound():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    synthetic_job = _workflow_job_text(workflow_text, "python-synthetic")
    run_script = _step_run_text(synthetic_job, "Run or skip synthetic partition")
    # Normalize PowerShell backtick continuations and whitespace into one line.
    normalized = run_script.replace("`", "").replace("\n", " ")
    normalized = " ".join(normalized.split())
    # Isolate the exact watchdog command and assert ordered components.
    watchdog_idx = normalized.find(
        "tests/fixtures/real_client_e2e/run-with-windows-watchdog.py"
    )
    timeout_idx = normalized.find("--timeout-seconds 3600")
    separator_idx = normalized.find("-- ")
    pytest_idx = normalized.find("python -m pytest")
    assert -1 < watchdog_idx < timeout_idx < separator_idx < pytest_idx, normalized


def test_ci_md_fallback_commands_use_watchdog_with_3600s_bound():
    ci_md = (ROOT / "docs" / "agents" / "ci.md").read_text(encoding="utf-8")
    # Both fallback blocks must run the synthetic module through the watchdog.
    assert "run-with-windows-watchdog.py" in ci_md
    assert "--timeout-seconds 3600" in ci_md
    # Direct unattended synthetic pytest must not remain in a fallback block.
    fallback_start = ci_md.find("## Full manual fallback")
    assert fallback_start != -1
    fallback_section = ci_md[fallback_start:]
    # Every synthetic pytest command in the fallback must be a continuation of
    # a watchdog invocation, not a direct unattended command.
    marker = "python -m pytest -q tests/test_real_client_e2e.py"
    pos = 0
    while True:
        idx = fallback_section.find(marker, pos)
        if idx == -1:
            break
        preceding = fallback_section[:idx]
        watchdog_pos = preceding.rfind("run-with-windows-watchdog.py")
        continuation_pos = preceding.rfind("-- `")
        assert watchdog_pos != -1, "synthetic pytest without watchdog in fallback section"
        assert watchdog_pos < continuation_pos < idx, (
            "unattended synthetic pytest in fallback section"
        )
        pos = idx + len(marker)


def test_ci_yaml_synthetic_job_has_no_depth_boundary():
    """Regression: re-shallowing base history can make merge-base fail."""
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    synthetic_job = _workflow_job_text(workflow_text, "python-synthetic")
    assert "--depth=" not in synthetic_job, "synthetic job must not re-shallow history"


def test_ci_yaml_changed_paths_uses_no_renames():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    synthetic_job = _workflow_job_text(workflow_text, "python-synthetic")
    plan_step = _step_run_text(synthetic_job, "Plan synthetic partition")
    assert "git diff" in plan_step
    assert "--no-renames" in plan_step
    assert "--name-only" in plan_step


def test_pr_rename_from_relevant_to_unrelated_runs_synthetic(plan):
    # With --no-renames git diff emits both the old relevant path and the new
    # unrelated path; the old path must keep the check synthetic.
    changed = ["tests/test_real_client_e2e.py", "src-python/codex_proxy.py"]
    p = plan.build_plan("pull_request", True, changed)
    assert p.synthetic_status == "run"


def test_pr_rename_from_unrelated_to_relevant_runs_synthetic(plan):
    # With --no-renames git diff emits both the old unrelated path and the new
    # relevant path; the new path must make the check synthetic.
    changed = ["src-python/codex_proxy.py", "tests/test_real_client_e2e.py"]
    p = plan.build_plan("pull_request", True, changed)
    assert p.synthetic_status == "run"


def test_ci_yaml_routes_final_jobs_to_repo_dedicated_self_hosted_labels():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    windows_jobs = (
        "python-core",
        "python-synthetic",
        "frontend",
        "rust-tests",
        "release-flavor-contract",
        "rust-clippy",
    )
    for job_name in windows_jobs:
        job = _workflow_job_text(workflow_text, job_name)
        assert (
            "runs-on: [self-hosted, Windows, X64, codexhub-ci-windows-x64]" in job
        ), job_name

    linux_job = _workflow_job_text(workflow_text, "rust-safe-file-linux")
    assert "runs-on: [self-hosted, Linux, X64, codexhub-ci-linux-x64]" in linux_job


def test_ci_yaml_preserves_final_check_names():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    expected_names = {
        "python-core": "Python core",
        "python-synthetic": "Synthetic real-client contract",
        "frontend": "Frontend build and UI contract",
        "rust-tests": "Rust tests (${{ matrix.flavor }})",
        "rust-safe-file-linux": "Rust safe_file Linux compile and tests",
        "release-flavor-contract": "Release flavor contract",
        "rust-clippy": "Rust clippy",
    }
    for job_name, check_name in expected_names.items():
        job = _workflow_job_text(workflow_text, job_name)
        assert f"name: {check_name}" in job, job_name


def test_ci_yaml_self_hosted_jobs_deny_fork_prs_and_smoke_only_dispatch():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    final_jobs = (
        "python-core",
        "python-synthetic",
        "frontend",
        "rust-tests",
        "rust-safe-file-linux",
        "release-flavor-contract",
        "rust-clippy",
    )
    for job_name in final_jobs:
        job = _workflow_job_text(workflow_text, job_name)
        assert "github.event.pull_request.head.repo.full_name == github.repository" in job
        assert "inputs.validation_scope != 'runner-smoke'" in job

    runner_doc = (
        ROOT / "docs" / "agents" / "self-hosted-runner.md"
    ).read_text(encoding="utf-8")
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED" in runner_doc
    assert "host-owned pre-job guard" in runner_doc
    assert "host hook is the authoritative boundary" in runner_doc
    assert "additional readable assertion" in runner_doc
    assert "head repositories both exactly" in runner_doc


def test_ci_yaml_has_bounded_windows_and_linux_runner_smoke_jobs():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "validation_scope:" in workflow_text
    assert "runner-smoke" in workflow_text

    windows_smoke = _workflow_job_text(workflow_text, "runner-smoke-windows")
    assert "name: Runner smoke (Windows)" in windows_smoke
    assert "runs-on: [self-hosted, Windows, X64, codexhub-ci-windows-x64]" in windows_smoke
    assert "timeout-minutes: 5" in windows_smoke
    assert "inputs.validation_scope == 'runner-smoke'" in windows_smoke

    linux_smoke = _workflow_job_text(workflow_text, "runner-smoke-linux")
    assert "name: Runner smoke (Linux)" in linux_smoke
    assert "runs-on: [self-hosted, Linux, X64, codexhub-ci-linux-x64]" in linux_smoke
    assert "timeout-minutes: 5" in linux_smoke
    assert "inputs.validation_scope == 'runner-smoke'" in linux_smoke
    assert "Rust 1.97.1 is required." in linux_smoke
    assert "clippy-x86_64-unknown-linux-gnu" in linux_smoke
    assert "x86_64-unknown-linux-musl" in linux_smoke
    assert "rust-lld" in linux_smoke

    assert "clippy-x86_64-pc-windows-msvc" in windows_smoke


def test_ci_yaml_linux_safe_file_uses_self_contained_linux_target():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    linux_job = _workflow_job_text(workflow_text, "rust-safe-file-linux")
    assert "rustup target list --installed" in linux_job
    assert "grep -qx 'x86_64-unknown-linux-musl'" in linux_job
    assert linux_job.count("--target x86_64-unknown-linux-musl") == 2
    assert linux_job.count("-C linker=rust-lld") == 2


def test_ci_yaml_windows_jobs_use_verified_preinstalled_toolchains():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    windows_python_jobs = (
        _workflow_job_text(workflow_text, "runner-smoke-windows"),
        _workflow_job_text(workflow_text, "python-core"),
        _workflow_job_text(workflow_text, "python-synthetic"),
    )
    for job in windows_python_jobs:
        assert "actions/setup-python" not in job
        assert "Python 3.13 is required." in job

    frontend_job = _workflow_job_text(workflow_text, "frontend")
    assert "actions/setup-node" not in frontend_job
    assert "Node.js 22 is required." in frontend_job

    for job_name in ("runner-smoke-windows", "rust-tests", "rust-clippy"):
        job = _workflow_job_text(workflow_text, job_name)
        assert "Rust 1.97.1 is required." in job


def test_ci_yaml_rust_jobs_do_not_use_remote_cargo_caches():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    rust_tests = _workflow_job_text(workflow_text, "rust-tests")
    assert "- normal" in rust_tests
    assert "- debug" in rust_tests

    for job_name in ("rust-tests", "rust-clippy"):
        job = _workflow_job_text(workflow_text, job_name)
        assert "actions/cache" not in job, job_name
        assert "Cache cargo artifacts" not in job, job_name
        assert "~/.cargo/registry" not in job, job_name
        assert "~/.cargo/git" not in job, job_name
        assert "src-tauri/target" not in job, job_name


def test_ci_yaml_final_jobs_have_explicit_safety_timeouts():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    for job_name in (
        "python-core",
        "python-synthetic",
        "frontend",
        "rust-tests",
        "rust-safe-file-linux",
        "release-flavor-contract",
        "rust-clippy",
    ):
        job = _workflow_job_text(workflow_text, job_name)
        assert re.search(r"(?m)^    timeout-minutes: \d+$", job), job_name


def test_ci_docs_type_unmanaged_paseo_checkout_as_not_applicable():
    ci_doc = (ROOT / "docs" / "agents" / "ci.md").read_text(encoding="utf-8")
    assert "not_applicable_unmanaged_checkout" in ci_doc
    assert "not a product regression" in ci_doc
