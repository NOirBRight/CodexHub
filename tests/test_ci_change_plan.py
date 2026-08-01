import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLANNER_PATH = ROOT / "scripts" / "ci" / "ci_change_plan.py"
LEGACY_PLANNER_PATH = ROOT / "scripts" / "ci" / "python_test_plan.py"


@pytest.fixture(scope="module")
def planner():
    spec = importlib.util.spec_from_file_location("ci_change_plan", PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def legacy_planner():
    spec = importlib.util.spec_from_file_location("python_test_plan", LEGACY_PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_plan_is_immutable_and_serializable(planner):
    plan = planner.build_plan("pull_request", True, ["frontend/src/App.tsx"])
    with pytest.raises((AttributeError, TypeError)):
        plan.frontend = False
    payload = json.loads(plan.to_json())
    assert payload["frontend"] is True
    assert payload["selected_jobs"] == ["frontend"]


def test_frontend_path_selects_only_frontend(planner):
    plan = planner.build_plan("pull_request", True, ["frontend/src/App.tsx"])
    assert plan.full is False
    assert plan.frontend is True
    assert plan.selected_jobs() == ("frontend",)


def test_shared_bridge_contract_selects_frontend_and_rust(planner):
    plan = planner.build_plan("pull_request", True, ["src-tauri/src/web_bridge.rs"])
    assert plan.frontend is True
    assert plan.rust is True
    assert plan.rust_safe_file_linux is False


def test_shared_catalog_contract_selects_frontend_and_rust(planner):
    plan = planner.build_plan("pull_request", True, ["src-tauri/src/catalog.rs"])
    assert plan.frontend is True
    assert plan.rust is True


@pytest.mark.parametrize(
    "path",
    [
        "src-tauri/src/openai_usage.rs",
        "src-tauri/src/official_refresh.rs",
        "src-tauri/capabilities/default.json",
        "src-tauri/Cargo.toml",
        "tests/fixtures/model_identity_vectors.json",
    ],
)
def test_ui_contract_external_input_selects_frontend(planner, path):
    plan = planner.build_plan("pull_request", True, [path])
    assert plan.frontend is True


def test_python_path_selects_core(planner):
    plan = planner.build_plan("pull_request", True, ["src-python/catalog_sync.py"])
    assert plan.selected_jobs() == ("python_core",)


def test_synthetic_fixture_selects_synthetic_only(planner):
    plan = planner.build_plan(
        "pull_request", True, ["tests/fixtures/real_client_e2e/fake-client.cmd"]
    )
    assert plan.selected_jobs() == ("python_synthetic",)


def test_synthetic_module_selects_synthetic_without_core(planner):
    plan = planner.build_plan("pull_request", True, ["tests/test_real_client_e2e.py"])
    assert plan.selected_jobs() == ("python_synthetic",)


def test_safe_file_selects_rust_and_linux_safe_file(planner):
    plan = planner.build_plan("pull_request", True, ["src-tauri/src/safe_file.rs"])
    assert plan.rust is True
    assert plan.rust_safe_file_linux is True
    assert plan.release_flavor is False


@pytest.mark.parametrize(
    "path",
    ["rust-toolchain", "rust-toolchain.toml", ".cargo/config", ".cargo/config.toml"],
)
def test_rust_control_paths_select_rust_and_linux_safe_file(planner, path):
    plan = planner.build_plan("pull_request", True, [path])
    assert plan.rust is True
    assert plan.rust_safe_file_linux is True


def test_unified_planner_covers_every_legacy_synthetic_dependency(
    planner, legacy_planner
):
    for path in legacy_planner.RELEVANT_SYNTHETIC_PATHS:
        probe = f"{path}fixture" if path.endswith("/") else path
        plan = planner.build_plan("pull_request", True, [probe])
        assert plan.python_synthetic, path
        assert plan.full is (path in planner.FULL_EXACT), path


def test_release_path_selects_release_and_rust(planner):
    plan = planner.build_plan("pull_request", True, ["src-tauri/tauri.conf.json"])
    assert plan.release_flavor is True
    assert plan.rust is True


def test_build_flavor_manifest_selects_release_and_python(planner):
    plan = planner.build_plan("pull_request", True, ["config/build-flavors.json"])
    assert plan.release_flavor is True
    assert plan.python_core is True


def test_update_contract_selects_release(planner):
    plan = planner.build_plan("pull_request", True, ["scripts/e2e-app-update.ps1"])
    assert plan.selected_jobs() == ("frontend", "release_flavor")


def test_installer_contract_doc_selects_release(planner):
    plan = planner.build_plan(
        "pull_request", True, ["docs/agents/windows-autostart-smoke.md"]
    )
    assert plan.selected_jobs() == ("release_flavor",)


def test_release_contract_tests_select_release(planner):
    plan = planner.build_plan(
        "pull_request", True, ["tests/test_release_channel_scripts.py"]
    )
    assert plan.release_flavor is True
    assert plan.python_core is True


def test_docs_only_selects_no_formal_jobs(planner):
    plan = planner.build_plan("pull_request", True, ["docs/notes/catalog.md"])
    assert plan.full is False
    assert plan.selected_jobs() == ()


@pytest.mark.parametrize("path", ["README.zh-CN.md", "CONTEXT.md"])
def test_root_documentation_selects_no_formal_jobs(planner, path):
    plan = planner.build_plan("pull_request", True, [path])
    assert plan.full is False
    assert plan.selected_jobs() == ()


@pytest.mark.parametrize("path", ["DESIGN.md", "docs/agents/user-feedback.md"])
def test_ui_contract_docs_select_frontend(planner, path):
    plan = planner.build_plan("pull_request", True, [path])
    assert plan.selected_jobs() == ("frontend",)


def test_contract_docs_select_full(planner):
    plan = planner.build_plan("pull_request", True, ["docs/agents/ci.md"])
    assert plan.full is True
    assert set(plan.selected_jobs()) == set(planner.JOB_KEYS)


def test_unknown_path_fails_closed_to_full(planner):
    plan = planner.build_plan("pull_request", True, ["mystery/new-format.bin"])
    assert plan.full is True
    assert plan.classifier_failed is False
    assert set(plan.selected_jobs()) == set(planner.JOB_KEYS)


@pytest.mark.parametrize("path", ["tests/fixtures/data.txt", "assets/schema.txt"])
def test_non_document_text_path_fails_closed_to_full(planner, path):
    plan = planner.build_plan("pull_request", True, [path])
    assert plan.full is True
    assert set(plan.selected_jobs()) == set(planner.JOB_KEYS)


def test_missing_paths_fails_classifier_and_selects_full(planner):
    plan = planner.build_plan("pull_request", True, None)
    assert plan.full is True
    assert plan.classifier_failed is True
    assert set(plan.selected_jobs()) == set(planner.JOB_KEYS)


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_non_pr_events_are_full(planner, event):
    plan = planner.build_plan(event, False, [])
    assert plan.full is True
    assert plan.classifier_failed is False


def test_cli_strict_returns_failure_for_unreadable_path_file(tmp_path):
    bad_path = tmp_path / "changed-paths"
    bad_path.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(PLANNER_PATH),
            "--event",
            "pull_request",
            "--is-pull-request",
            "--changed-paths-file",
            str(bad_path),
            "--output-json",
            "--strict",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["classifier_failed"] is True
