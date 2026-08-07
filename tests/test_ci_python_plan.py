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
    match = re.search(rf"(?m)^  {re.escape(job_name)}:", workflow_text)
    assert match is not None, f"job {job_name!r} not found"
    start = match.start()
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
    pytest_idx = normalized.find("python.exe -m pytest")
    assert -1 < watchdog_idx < timeout_idx < separator_idx < pytest_idx, normalized


def test_ci_md_fallback_commands_use_watchdog_with_3600s_bound():
    ci_md = (ROOT / "docs" / "agents" / "ci.md").read_text(encoding="utf-8")
    # Both fallback blocks must run the synthetic module through the watchdog.
    assert "run-with-windows-watchdog.py" in ci_md
    assert "--timeout-seconds 3600" in ci_md
    # Direct unattended synthetic pytest must not remain in a fallback block.
    fallback_start = ci_md.find("## Full local verification matrix")
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
    """Synthetic checkout remains deep for reproducible fixture execution."""
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    synthetic_job = _workflow_job_text(workflow_text, "python-synthetic")
    assert "--depth=" not in synthetic_job, "synthetic job must not re-shallow history"


def test_ci_yaml_classifier_uses_immutable_change_plan():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    classifier_job = _workflow_job_text(workflow_text, "classifier")
    assert "fetch-depth: 0" in classifier_job
    plan_step = _step_run_text(classifier_job, "Classify changed paths")
    assert "scripts/ci/ci_change_plan.py" in plan_step
    assert "--output-json" in plan_step
    assert "Fail closed when path acquisition failed" in classifier_job
    assert "CI_PR_BASE_SHA" in classifier_job


def test_ci_yaml_rust_jobs_force_the_declared_toolchain():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "RUSTUP_TOOLCHAIN: 1.97.1" in workflow_text


def test_ci_md_describes_hosted_linux_and_unified_planner_verification():
    ci_md = (ROOT / "docs" / "agents" / "ci.md").read_text(encoding="utf-8")
    assert "Ubuntu WSL2" not in ci_md
    assert "ubuntu-24.04" in ci_md
    planner_start = ci_md.find("To verify only the planner/path logic")
    assert planner_start != -1
    planner_section = ci_md[planner_start:]
    assert "tests/test_ci_change_plan.py" in planner_section


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


def test_ci_yaml_routes_final_jobs_to_github_hosted_runners():
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
        assert "runs-on: windows-2025" in job, job_name

    linux_job = _workflow_job_text(workflow_text, "rust-safe-file-linux")
    assert "runs-on: ubuntu-24.04" in linux_job
    assert "self-hosted" not in workflow_text


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


def test_ci_yaml_hosted_jobs_have_no_self_hosted_guards_or_smoke_dispatch():
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
        assert "github.event.pull_request.head.repo.full_name" not in job
        assert "runner-smoke" not in job

    assert "validation_scope" not in workflow_text
    assert "runner-smoke" not in workflow_text
    assert "workflow_dispatch:" in workflow_text


def test_ci_yaml_has_classifier_and_single_gate():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    classifier = _workflow_job_text(workflow_text, "classifier")
    assert "name: CI classifier" in classifier
    assert "runs-on: windows-2025" in classifier
    assert "ci_change_plan.py" in classifier

    gate = _workflow_job_text(workflow_text, "gate")
    assert "name: CI / gate" in gate
    assert "if: always()" in gate
    for job_name in (
        "classifier",
        "python-core",
        "python-synthetic",
        "frontend",
        "rust-tests",
        "rust-safe-file-linux",
        "release-flavor-contract",
        "rust-clippy",
    ):
        assert f"- {job_name}" in gate


def test_ci_yaml_never_uses_workflow_paths_filters():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "paths:" not in workflow_text
    assert "paths-ignore:" not in workflow_text


def test_ci_yaml_has_no_runner_smoke_jobs():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "runner-smoke-windows" not in workflow_text
    assert "runner-smoke-linux" not in workflow_text


def test_ci_yaml_linux_safe_file_uses_self_contained_linux_target():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    linux_job = _workflow_job_text(workflow_text, "rust-safe-file-linux")
    assert "rustup target list --installed" in linux_job
    assert "grep -qx 'x86_64-unknown-linux-musl'" in linux_job
    assert linux_job.count("--target x86_64-unknown-linux-musl") == 2
    assert linux_job.count("-C linker=rust-lld") == 2


def test_ci_yaml_hosted_jobs_install_pinned_toolchains():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    for job_name in ("python-core", "python-synthetic"):
        job = _workflow_job_text(workflow_text, job_name)
        assert "actions/setup-python@v7" in job
        assert "python-version: '3.13'" in job

    frontend_job = _workflow_job_text(workflow_text, "frontend")
    assert "actions/setup-node@v7" in frontend_job
    assert "node-version: '22'" in frontend_job

    for job_name in ("rust-tests", "rust-clippy"):
        job = _workflow_job_text(workflow_text, job_name)
        assert "rustup toolchain install" in job
        assert "1.97.1" in job

    linux_job = _workflow_job_text(workflow_text, "rust-safe-file-linux")
    assert "actions/setup-python@v7" in linux_job
    assert "rustup toolchain install" in linux_job


def test_ci_yaml_rust_caches_exclude_build_outputs():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    expected_key_prefixes = {
        "rust-tests": "cargo-deps-v2-test-",
        "rust-clippy": "cargo-deps-v2-clippy-",
    }
    for job_name, key_prefix in expected_key_prefixes.items():
        job = _workflow_job_text(workflow_text, job_name)
        assert "uses: actions/cache@v6" in job
        assert "~/.cargo/registry" in job
        assert "~/.cargo/git" in job
        assert key_prefix in job
        assert "src-tauri/target" not in job


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


def test_ci_yaml_uses_current_official_action_majors():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "actions/checkout@v7" in workflow_text
    assert "actions/setup-python@v7" in workflow_text
    assert "actions/setup-node@v7" in workflow_text
    assert "actions/cache@v6" in workflow_text
    assert "actions/upload-artifact@v7" in workflow_text


def test_ci_yaml_rust_jobs_force_serial_test_execution():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    rust_job = _workflow_job_text(workflow_text, "rust-tests")
    assert "--test-threads=1" in rust_job
