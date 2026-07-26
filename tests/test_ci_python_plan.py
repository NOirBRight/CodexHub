import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


try:
    import yaml

    _HAS_YAML = True
except Exception:  # pragma: no cover
    _HAS_YAML = False

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


@pytest.mark.skipif(not _HAS_YAML, reason="PyYAML not installed")
def test_ci_yaml_has_full_checkout_for_synthetic_merge_base():
    """Regression: shallow checkout breaks git merge-base on a fresh PR runner."""
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    synthetic_job = workflow["jobs"]["python-synthetic"]
    checkout_steps = [
        step
        for step in synthetic_job["steps"]
        if isinstance(step, dict) and step.get("name") == "Check out repository"
    ]
    assert len(checkout_steps) == 1
    checkout = checkout_steps[0]
    assert checkout.get("uses", "").startswith("actions/checkout")
    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_ci_yaml_synthetic_run_uses_watchdog_with_3600s_bound():
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    # The synthetic run step must invoke the checked-in watchdog and use 3600s.
    assert "run-with-windows-watchdog.py" in workflow_text
    assert "--timeout-seconds 3600" in workflow_text
    # Direct unattended pytest of the synthetic module is not allowed.
    synthetic_step = workflow_text.split("Run or skip synthetic partition")[1]
    assert "python -m pytest" in synthetic_step
    assert "run-with-windows-watchdog.py" in synthetic_step


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
