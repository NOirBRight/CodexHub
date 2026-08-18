from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SELECTOR = ROOT / "scripts" / "Resolve-CodexHubPython.ps1"
LAUNCHER = ROOT / "scripts" / "codexhub-python.ps1"
CMD_LAUNCHER = ROOT / "scripts" / "codexhub-python.cmd"
ACTIVATION = ROOT / "scripts" / "Enter-CodexHubPython.ps1"
PREPARE_RUNTIME = ROOT / "scripts" / "Prepare-PythonRuntime.ps1"

DIRECT_PYTHON_ENTRYPOINTS = (
    "src-python/bucket_sync.py",
    "src-python/catalog_sync.py",
    "src-python/codex_proxy.py",
    "src-python/config_overlay.py",
    "src-python/global_state_repair.py",
    "src-python/history_consolidate.py",
    "src-python/history_overlay.py",
    "src-python/probe_upstream_format.py",
    "scripts/analyze_transport_failures.py",
    "scripts/audit_issue_62_runtime_artifacts.py",
    "scripts/beta42_evidence.py",
    "scripts/build_issue_392_collaboration_contract.py",
    "scripts/build_issue_62_control_manifest.py",
    "scripts/build_issue_62_runtime_inventory.py",
    "scripts/build_issue_64_collaboration_inventory.py",
    "scripts/capture_issue_392_collaboration_runtime.py",
    "scripts/capture_issue_62_live_evidence.py",
    "scripts/check_codex_task_creation_lifecycle.py",
    "scripts/ci/check_python_test_partitions.py",
    "scripts/ci/ci_change_plan.py",
    "scripts/ci/python_test_plan.py",
    "scripts/e2e_codex_active_call_regression.py",
    "scripts/e2e_codex_app_transport.py",
    "scripts/e2e_codex_catalog_roundtrip.py",
    "scripts/e2e_gateway_client_matrix.py",
    "scripts/e2e_history_online_sync.py",
    "scripts/generate_wayfinder_final_audit.py",
    "scripts/issue_278_fixture_mcp.py",
    "scripts/qualify_beta3_protocol_cli.py",
    "scripts/replay_official_transport.py",
    "scripts/report_quality_gates.py",
    "scripts/run_claude_messages_spike_smoke.py",
    "scripts/run_issue_106_task_lifecycle.py",
    "scripts/run_issue_283_app_v2_request_shape.py",
    "scripts/run_issue_283_cli_v2_lifecycle.py",
    "scripts/run_issue_62_live_control.py",
    "scripts/validate_issue_278_evidence.py",
    "scripts/validate_issue_369_matrix.py",
    "scripts/validate_issue_63_evidence.py",
    "tests/validate_issue_108_evidence.py",
    "tests/validate_issue_251_evidence.py",
)

DIRECT_FIXTURE_PYTHON_ENTRYPOINTS = (
    "tests/fixtures/real_client_e2e/fake-debug-gateway.py",
    "tests/fixtures/real_client_e2e/fake-gui-expanding-tree.py",
    "tests/fixtures/real_client_e2e/fake-managed-client-config.py",
    "tests/fixtures/real_client_e2e/fake-watchdog-child.py",
    "tests/fixtures/real_client_e2e/run-with-windows-watchdog.py",
    "tests/fixtures/real_client_e2e/validate-client-routing.py",
    "tests/fixtures/real_client_e2e/validate-managed-client-contract-probe.py",
    "tests/fixtures/real_client_e2e/validate-zcode-structures.py",
    "tests/fixtures/real_client_e2e/write-catalog.py",
)


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is required for the repository Python launcher")
    return executable


def _find_incompatible_python() -> str | None:
    """Find a real pre-3.13 interpreter even when the wrapper changed PATH."""

    candidates: list[str] = []
    if os.name == "nt":
        where = shutil.which("where.exe")
        if where:
            result = subprocess.run(
                [where, "python"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            candidates.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    else:
        python = shutil.which("python")
        if python:
            candidates.append(python)

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(Path(candidate).resolve()).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        result = subprocess.run(
            [candidate, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            continue
        try:
            major, minor = (int(part) for part in result.stdout.strip().split(".", 1))
        except (ValueError, TypeError):
            continue
        if (major, minor) < (3, 13):
            return candidate
    return None


def _direct_entrypoints() -> list[Path]:
    entrypoints: list[Path] = []
    for root in (ROOT / "src-python", ROOT / "scripts", ROOT / "tests"):
        for path in root.rglob("*.py"):
            if "scripts" in path.parts and "tests" in path.parts:
                continue
            if "tests" in path.parts and "fixtures" in path.parts:
                continue
            if "tests" in path.parts and path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
                and any(
                    isinstance(comparator, ast.Constant)
                    and comparator.value == "__main__"
                    for comparator in node.test.comparators
                )
                for node in ast.walk(tree)
            ):
                entrypoints.append(path)
    return sorted(entrypoints)


def _run_script(script: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-RepoRoot",
            str(ROOT),
            *arguments,
        ],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _run_relative_powershell_script(
    relative_script: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Run a repository script the same way a fresh Windows shell does."""

    return subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            relative_script,
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _write_python_without_pytest(tmp_path: Path) -> Path:
    wrapper = tmp_path / "python-without-pytest.cmd"
    wrapper.write_text(
        "\r\n".join(
            [
                "@echo off",
                "if /I \"%~1\"==\"-m\" if /I \"%~2\"==\"pytest\" goto missing_pytest",
                "if /I \"%~1\"==\"-c\" if /I \"%~2\"==\"import pytest\" goto missing_pytest",
                f'\"{sys.executable}\" %*',
                "exit /b %errorlevel%",
                ":missing_pytest",
                "echo No module named pytest 1>&2",
                "exit /b 1",
                "",
            ]
        ),
        encoding="ascii",
    )
    return wrapper


def test_repository_selector_returns_python_313_or_newer() -> None:
    result = _run_script(SELECTOR, "-PrintPath")
    assert result.returncode == 0, result.stdout + result.stderr
    path = result.stdout.strip().splitlines()[-1]
    version = subprocess.check_output(
        [path, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()
    major, minor = (int(part) for part in version.split(".", 1))
    assert (major, minor) >= (3, 13)


def test_relative_windows_entrypoints_keep_the_repository_runtime_contract() -> None:
    """Windows PowerShell 5.1 must not lose script-root defaults on -File."""

    selector = _run_relative_powershell_script(
        r".\scripts\Resolve-CodexHubPython.ps1", "-PrintPath"
    )
    assert selector.returncode == 0, selector.stdout + selector.stderr
    selected = Path(selector.stdout.strip().splitlines()[-1])
    assert selected.is_file()
    assert "Python313" in str(selected) or "python.exe" in selected.name.lower()

    launcher = _run_relative_powershell_script(
        r".\scripts\codexhub-python.ps1",
        "-c",
        "import sys; print(sys.version_info[:2])",
    )
    assert launcher.returncode == 0, launcher.stdout + launcher.stderr
    assert "(3, 13)" in launcher.stdout or "(3, 14)" in launcher.stdout

    portable_plan = _run_relative_powershell_script(
        r".\scripts\build-windows-portable.ps1",
        "-Flavor",
        "normal",
        "-DryRun",
    )
    assert portable_plan.returncode == 0, portable_plan.stdout + portable_plan.stderr
    assert '"version":"0.1.9-beta.1.6"' in portable_plan.stdout

    runtime_check = _run_relative_powershell_script(
        r".\scripts\Prepare-PythonRuntime.ps1", "-CheckOnly"
    )
    assert runtime_check.returncode == 0, runtime_check.stdout + runtime_check.stderr
    assert "Python runtime check passed" in runtime_check.stdout


def test_fresh_shell_pytest_selector_skips_a_3_13_runtime_without_pytest() -> None:
    """Fresh PATH order must not strand test commands on the bundled runtime."""

    bundled = ROOT / "src-tauri" / "resources" / "python" / "python.exe"
    if not bundled.is_file():
        pytest.skip("the prepared bundled Python runtime is unavailable")

    host_directory = Path(sys.executable).resolve().parent
    path_entries = [bundled.parent, host_directory]
    path_entries.extend(
        Path(entry)
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() not in {entry.resolve() for entry in path_entries}
    )
    child_env = {
        "PATH": os.pathsep.join(str(entry) for entry in path_entries),
        "CODEXHUB_E2E_PYTHON": "",
        "CODEXHUB_PYTHON": "",
        "CODEXHUB_PROXY_PYTHON": "",
    }
    result = _run_script(SELECTOR, "-PrintPath", "-RequirePytest", env=child_env)
    assert result.returncode == 0, result.stdout + result.stderr
    selected = Path(result.stdout.strip().splitlines()[-1]).resolve()
    assert selected != bundled.resolve()
    probe = subprocess.run(
        [str(selected), "-c", "import pytest; print('pytest-ready')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "pytest-ready" in probe.stdout


def test_every_direct_python_entrypoint_declares_the_runtime_preflight() -> None:
    entrypoints = _direct_entrypoints()
    assert entrypoints
    assert {
        str(path.relative_to(ROOT)).replace(os.sep, "/") for path in entrypoints
    } == set(DIRECT_PYTHON_ENTRYPOINTS)
    missing = [
        str(path.relative_to(ROOT))
        for path in entrypoints
        if "require_python_313" not in path.read_text(encoding="utf-8")
    ]
    assert missing == []


def test_every_direct_fixture_python_entrypoint_declares_the_runtime_preflight() -> None:
    missing = [
        entrypoint
        for entrypoint in DIRECT_FIXTURE_PYTHON_ENTRYPOINTS
        if "require_python_313" not in (ROOT / entrypoint).read_text(encoding="utf-8")
    ]
    assert missing == []


def test_prepare_runtime_check_is_compatible_with_windows_powershell_51() -> None:
    """The packaged-runtime preflight must not depend on PS 7 quoting rules."""
    powershell_51 = shutil.which("powershell.exe")
    if powershell_51 is None:
        pytest.skip("Windows PowerShell 5.1 is required for this compatibility check")

    result = subprocess.run(
        [
            powershell_51,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PREPARE_RUNTIME),
            "-RepoRoot",
            str(ROOT),
            "-CheckOnly",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Python runtime check passed" in result.stdout


def test_prepare_runtime_check_ignores_host_python_environment(tmp_path: Path) -> None:
    """Packaged-runtime validation must not inherit a host Python prefix."""
    result = _run_script(
        PREPARE_RUNTIME,
        "-CheckOnly",
        env={
            "PYTHONHOME": str(tmp_path / "hermes-3.11"),
            "PYTHONPATH": str(tmp_path / "hermes-3.11" / "site-packages"),
            "VIRTUAL_ENV": str(tmp_path / "hermes-3.11"),
            "CONDA_PREFIX": str(tmp_path / "hermes-3.11" / "conda"),
            "PIPENV_ACTIVE": "1",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Python runtime check passed" in result.stdout


def test_repository_selector_resolves_under_host_runtime_selector_contamination(
    tmp_path: Path,
) -> None:
    result = _run_script(
        SELECTOR,
        "-PrintPath",
        env={
            "PYTHONHOME": str(tmp_path / "hermes-3.11"),
            "VIRTUAL_ENV": str(tmp_path / "hermes-3.11"),
            "CONDA_PREFIX": str(tmp_path / "conda-3.11"),
            "PIPENV_ACTIVE": "1",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    path = result.stdout.strip().splitlines()[-1]
    version = subprocess.check_output(
        [path, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()
    major, minor = (int(part) for part in version.split(".", 1))
    assert (major, minor) >= (3, 13)


def test_repository_selector_can_explicitly_select_the_bundled_runtime() -> None:
    bundled = ROOT / "src-tauri" / "resources" / "python" / "python.exe"
    if not bundled.is_file():
        pytest.skip("the prepared bundled Python runtime is unavailable")

    child_env = os.environ.copy()
    for name in ("CODEXHUB_E2E_PYTHON", "CODEXHUB_PYTHON", "CODEXHUB_PROXY_PYTHON"):
        child_env[name] = ""
    result = _run_script(
        SELECTOR,
        "-PrintPath",
        "-PreferBundled",
        env=child_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == bundled.resolve()


def test_repository_selector_honors_the_e2e_runtime_binding(tmp_path: Path) -> None:
    selected = _write_python_without_pytest(tmp_path)
    conflicting = tmp_path / "conflicting-python.cmd"
    conflicting.write_text(
        f'@echo off\r\n"{sys.executable}" %*\r\nexit /b %errorlevel%\r\n',
        encoding="ascii",
    )
    result = _run_script(
        SELECTOR,
        "-PrintPath",
        env={
            "CODEXHUB_E2E_PYTHON": str(selected),
            "CODEXHUB_PYTHON": str(conflicting),
            "CODEXHUB_PROXY_PYTHON": "",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == selected.resolve()


def test_repository_launcher_can_import_python_313_syntax_source() -> None:
    result = _run_script(
        LAUNCHER,
        "-c",
        "from providers_config import _sort_by_order; import sys; print(sys.version_info[:2])",
        env={"PYTHONPATH": str(ROOT / "src-python")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "(3, 13)" in result.stdout or "(3, 14)" in result.stdout


def test_repository_launcher_uses_the_development_runtime_for_tools() -> None:
    bundled = ROOT / "src-tauri" / "resources" / "python" / "python.exe"

    child_env = os.environ.copy()
    for name in ("CODEXHUB_E2E_PYTHON", "CODEXHUB_PYTHON", "CODEXHUB_PROXY_PYTHON"):
        child_env.pop(name, None)
    result = subprocess.run(
        [
            str(CMD_LAUNCHER),
            "-m",
            "pytest",
            "--version",
        ],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pytest" in result.stdout.lower()
    if bundled.is_file():
        assert str(bundled.resolve()) not in result.stdout


def test_repository_launcher_keeps_sys_executable_pytest_children_on_development_runtime(
    tmp_path: Path,
) -> None:
    """A script invoking ``sys.executable -m pytest`` must not get the embedded runtime."""

    script = tmp_path / "pytest-child.py"
    script.write_text(
        "import subprocess, sys\n"
        "result = subprocess.run([sys.executable, '-m', 'pytest', '--version'], "
        "capture_output=True, text=True)\n"
        "print(result.stdout or result.stderr, end='')\n"
        "raise SystemExit(result.returncode)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(CMD_LAUNCHER), str(script)],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pytest" in result.stdout


def test_repository_launcher_exports_one_interpreter_to_all_children() -> None:
    result = _run_script(
        LAUNCHER,
        "-c",
        "import os, sys; print(sys.executable); print(os.environ['CODEXHUB_PYTHON']); print(os.environ['CODEXHUB_PROXY_PYTHON']); print(os.environ['CODEXHUB_E2E_PYTHON'])",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) >= 4
    assert Path(lines[-4]).resolve() == Path(lines[-3]).resolve()
    assert Path(lines[-4]).resolve() == Path(lines[-2]).resolve()
    assert Path(lines[-4]).resolve() == Path(lines[-1]).resolve()


def test_repository_launcher_removes_host_runtime_selection_variables(
    tmp_path: Path,
) -> None:
    child_env = os.environ.copy()
    child_env.update(
        {
            "PYTHONHOME": str(tmp_path / "hermes-3.11"),
            "PYTHONSTARTUP": str(tmp_path / "startup.py"),
            "VIRTUAL_ENV": str(tmp_path / "hermes-3.11"),
            "CONDA_PREFIX": str(tmp_path / "conda-3.11"),
            "CONDA_DEFAULT_ENV": "hermes",
            "PIPENV_ACTIVE": "1",
        }
    )
    result = subprocess.run(
        [
            str(CMD_LAUNCHER),
            "-c",
            "import os, sys; print(sys.version_info[:2]); print([os.environ.get(name) for name in ('PYTHONHOME', 'PYTHONSTARTUP', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'CONDA_DEFAULT_ENV', 'PIPENV_ACTIVE')])",
        ],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "(3, 13)" in result.stdout or "(3, 14)" in result.stdout
    assert "[None, None, None, None, None, None]" in result.stdout


def test_repository_launcher_puts_selected_interpreter_first_on_child_path() -> None:
    result = _run_script(
        LAUNCHER,
        "-c",
        "import os, shutil, sys; path=os.environ['PATH'].split(os.pathsep); print(sys.executable); print(shutil.which('python')); print(shutil.which('pytest')); print(path[0]); print(path[1])",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) >= 5
    assert Path(lines[-2]).resolve() == Path(lines[-4]).parent.resolve()
    assert Path(lines[-4]).resolve() == Path(lines[-5]).resolve()
    assert Path(lines[-3]).resolve().parent in {
        Path(lines[-4]).resolve().parent,
        (ROOT / "scripts").resolve(),
    }
    assert Path(lines[-1]).resolve() == (ROOT / "scripts").resolve()


def test_cmd_launcher_puts_selected_interpreter_first_on_child_path() -> None:
    child_env = os.environ.copy()
    for name in ("CODEXHUB_E2E_PYTHON", "CODEXHUB_PROXY_PYTHON"):
        child_env.pop(name, None)
    child_env["CODEXHUB_PYTHON"] = sys.executable
    result = subprocess.run(
        [str(CMD_LAUNCHER), "-c", "import os, shutil, sys; path=os.environ['PATH'].split(os.pathsep); print(sys.executable); print(shutil.which('python')); print(shutil.which('pytest')); print(path[0]); print(path[1])"],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) >= 5
    assert Path(lines[-2]).resolve() == Path(lines[-4]).parent.resolve()
    assert Path(lines[-4]).resolve() == Path(lines[-5]).resolve()
    assert Path(lines[-3]).resolve().parent in {
        Path(lines[-4]).resolve().parent,
        (ROOT / "scripts").resolve(),
    }
    assert Path(lines[-1]).resolve() == (ROOT / "scripts").resolve()


def test_cmd_launcher_exports_one_interpreter_to_all_children() -> None:
    result = subprocess.run(
        [
            str(CMD_LAUNCHER),
            "-c",
            "import os, sys; print(sys.executable); print(os.environ['CODEXHUB_PYTHON']); print(os.environ['CODEXHUB_PROXY_PYTHON']); print(os.environ['CODEXHUB_E2E_PYTHON'])",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) >= 4
    assert Path(lines[-4]).resolve() == Path(lines[-3]).resolve()
    assert Path(lines[-4]).resolve() == Path(lines[-2]).resolve()
    assert Path(lines[-4]).resolve() == Path(lines[-1]).resolve()


def test_cmd_launcher_replaces_ambient_pythonpath_with_repository_import_root(
    tmp_path: Path,
) -> None:
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(tmp_path / "hermes-3.11" / "site-packages")
    result = subprocess.run(
        [str(CMD_LAUNCHER), "-c", "import os; print(os.environ['PYTHONPATH'])"],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == (
        ROOT / "src-python"
    ).resolve()


def test_pytest_command_rejects_a_compatible_interpreter_without_pytest(
    tmp_path: Path,
) -> None:
    """A Python 3.13 executable without pytest must fail at the launcher boundary."""

    wrapper = _write_python_without_pytest(tmp_path)
    child_env = os.environ.copy()
    child_env["CODEXHUB_PYTHON"] = str(wrapper)
    child_env.pop("CODEXHUB_PROXY_PYTHON", None)
    child_env.pop("CODEXHUB_E2E_PYTHON", None)

    result = subprocess.run(
        [str(CMD_LAUNCHER), "-m", "pytest", "--version"],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CodexHub test runtime requires pytest" in combined


def test_powershell_launcher_rejects_a_compatible_interpreter_without_pytest(
    tmp_path: Path,
) -> None:
    wrapper = _write_python_without_pytest(tmp_path)
    result = _run_script(
        LAUNCHER,
        "-m",
        "pytest",
        "--version",
        env={
            "CODEXHUB_PYTHON": str(wrapper),
            "CODEXHUB_PROXY_PYTHON": "",
            "CODEXHUB_E2E_PYTHON": "",
        },
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CodexHub test runtime requires pytest" in combined


def test_interactive_activation_rebinds_bare_python_and_pytest() -> None:
    selected_directory = Path(sys.executable).resolve().parent
    managed_directories = {selected_directory, (ROOT / "scripts").resolve()}
    child_env = os.environ.copy()
    child_env.pop("CODEXHUB_PYTHON", None)
    child_env.pop("CODEXHUB_PROXY_PYTHON", None)
    child_env["PATH"] = os.pathsep.join(
        entry
        for entry in child_env["PATH"].split(os.pathsep)
        if entry and Path(entry).resolve() not in managed_directories
    )
    command = (
        f". '{ACTIVATION}'; "
        'python -c "import shutil, sys; print(sys.version_info[:2]); print(sys.executable); print(shutil.which(\'pytest\'))"'
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines[-3] in {"(3, 13)", "(3, 14)"}
    assert Path(lines[-2]).is_file()
    assert Path(lines[-1]).resolve().parent in {
        Path(lines[-2]).resolve().parent,
        (ROOT / "scripts").resolve(),
    }


@pytest.mark.parametrize("shell_name", ["pwsh", "powershell.exe"])
def test_interactive_activation_accepts_relative_dot_source(shell_name: str) -> None:
    """The documented relative activation command must bind the caller shell."""

    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is required for the activation check")

    command = (
        ". .\\scripts\\Enter-CodexHubPython.ps1; "
        'python -c "import sys; print(sys.version_info[:2]); print(sys.executable)"'
    )
    managed_directories = {
        Path(sys.executable).resolve().parent,
        (ROOT / "scripts").resolve(),
    }
    child_env = os.environ.copy()
    for name in ("CODEXHUB_E2E_PYTHON", "CODEXHUB_PYTHON", "CODEXHUB_PROXY_PYTHON"):
        child_env.pop(name, None)
    child_env["PATH"] = os.pathsep.join(
        entry
        for entry in child_env["PATH"].split(os.pathsep)
        if entry and Path(entry).resolve() not in managed_directories
    )
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines[-2] in {"(3, 13)", "(3, 14)"}
    assert Path(lines[-1]).is_file()


def test_interactive_activation_rejects_non_dot_sourced_invocation() -> None:
    """A child PowerShell cannot change the caller's PATH silently."""

    result = _run_script(ACTIVATION)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "dot-source" in combined
    assert "scripts\\Enter-CodexHubPython.ps1" in combined


def test_repository_launcher_preserves_a_script_path_as_the_first_argument(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "argument-probe.py"
    probe.write_text("import sys; print(sys.argv[1:])\n", encoding="utf-8")
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(LAUNCHER),
            str(probe),
            "alpha",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "['alpha']" in result.stdout


def test_cmd_launcher_preserves_separator_and_script_arguments(tmp_path: Path) -> None:
    probe = tmp_path / "argument-probe.py"
    probe.write_text("import sys; print(sys.argv[1:])\n", encoding="utf-8")
    result = subprocess.run(
        [str(CMD_LAUNCHER), str(probe), "alpha", "--", "omega"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "['alpha', '--', 'omega']" in result.stdout


def test_watchdog_launcher_requires_a_pytest_capable_runtime() -> None:
    """The watchdog must bind a runtime that can execute its pytest child."""
    watchdog = ROOT / "tests" / "fixtures" / "real_client_e2e" / "run-with-windows-watchdog.py"
    result = subprocess.run(
        [
            str(CMD_LAUNCHER),
            str(watchdog),
            "--timeout-seconds",
            "10",
            "--",
            str(CMD_LAUNCHER),
            "-m",
            "pytest",
            "--version",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pytest" in result.stdout.lower()


def test_repository_selector_rejects_an_explicit_python_311_override() -> None:
    ambient = _find_incompatible_python()
    if ambient is None:
        pytest.skip("an incompatible Python executable is unavailable")

    result = _run_script(
        SELECTOR,
        "-PrintPath",
        env={
            "CODEXHUB_E2E_PYTHON": "",
            "CODEXHUB_PYTHON": ambient,
            "CODEXHUB_PROXY_PYTHON": "",
        },
    )
    assert result.returncode != 0
    assert re.search(r"interpreter\s+is\s+not.*compatible", result.stdout + result.stderr, re.DOTALL)


def test_repository_selector_rejects_an_incompatible_proxy_override_without_fallback() -> None:
    ambient = _find_incompatible_python()
    if ambient is None:
        pytest.skip("an incompatible Python executable is unavailable")

    result = _run_script(
        SELECTOR,
        "-PrintPath",
        env={
            "CODEXHUB_E2E_PYTHON": "",
            "CODEXHUB_PYTHON": "",
            "CODEXHUB_PROXY_PYTHON": ambient,
        },
    )
    assert result.returncode != 0
    assert re.search(r"interpreter\s+is\s+not.*compatible", result.stdout + result.stderr, re.DOTALL)


def test_fixture_launcher_rejects_an_incompatible_proxy_override(tmp_path: Path) -> None:
    cmd = shutil.which("cmd.exe")
    if cmd is None:
        pytest.skip("Windows cmd.exe is required for the fixture launcher")

    incompatible = tmp_path / "python311.cmd"
    incompatible.write_text("@echo off\nexit /b 1\n", encoding="ascii")
    fixture = ROOT / "tests" / "fixtures" / "real_client_e2e" / "run-fixture-python.cmd"
    child_env = os.environ.copy()
    for name in ("CODEXHUB_E2E_PYTHON", "CODEXHUB_PYTHON", "CODEXHUB_PROXY_PYTHON"):
        child_env.pop(name, None)
    child_env["CODEXHUB_PROXY_PYTHON"] = str(incompatible)
    result = subprocess.run(
        [cmd, "/d", "/c", str(fixture), "-c", "pass"],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 126
    assert "requires Python 3.13 or newer" in result.stderr


def test_fixture_launcher_requires_an_explicit_runtime_binding() -> None:
    cmd = shutil.which("cmd.exe")
    if cmd is None:
        pytest.skip("Windows cmd.exe is required for the fixture launcher")

    fixture = ROOT / "tests" / "fixtures" / "real_client_e2e" / "run-fixture-python.cmd"
    child_env = os.environ.copy()
    for name in ("CODEXHUB_E2E_PYTHON", "CODEXHUB_PYTHON", "CODEXHUB_PROXY_PYTHON"):
        child_env.pop(name, None)
    result = subprocess.run(
        [cmd, "/d", "/c", str(fixture), "-c", "pass"],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 127
    assert "requires an explicit CODEXHUB_E2E_PYTHON binding" in result.stderr


def test_pytest_preflight_rejects_python_311_before_source_collection() -> None:
    ambient = _find_incompatible_python()
    if ambient is None:
        pytest.skip("an incompatible Python executable is unavailable")

    result = subprocess.run(
        [ambient, "-m", "pytest", "--collect-only", "-q", "tests/test_providers_config.py"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src-python")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CodexHub requires Python 3.13 or newer" in combined


@pytest.mark.parametrize("entrypoint", ["codex_proxy.py", "catalog_sync.py"])
def test_core_python_entrypoints_reject_python_311_before_importing_313_modules(
    entrypoint: str,
) -> None:
    ambient = _find_incompatible_python()
    if ambient is None:
        pytest.skip("an incompatible Python executable is unavailable")

    result = subprocess.run(
        [ambient, str(ROOT / "src-python" / entrypoint), "--help"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src-python")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CodexHub requires Python 3.13 or newer" in combined


@pytest.mark.parametrize("entrypoint", DIRECT_PYTHON_ENTRYPOINTS)
def test_every_direct_python_entrypoint_rejects_ambient_python_311_before_work(
    entrypoint: str,
) -> None:
    """No direct utility may bypass the single runtime contract."""

    ambient = _find_incompatible_python()
    if ambient is None:
        pytest.skip("an incompatible Python executable is unavailable")

    result = subprocess.run(
        [ambient, str(ROOT / entrypoint), "--help"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src-python")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CodexHub requires Python 3.13 or newer" in combined


@pytest.mark.parametrize("entrypoint", DIRECT_FIXTURE_PYTHON_ENTRYPOINTS)
def test_every_direct_fixture_entrypoint_rejects_ambient_python_311_before_work(
    entrypoint: str,
) -> None:
    ambient = _find_incompatible_python()
    if ambient is None:
        pytest.skip("an incompatible Python executable is unavailable")

    result = subprocess.run(
        [ambient, str(ROOT / entrypoint), "--help"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "CodexHub requires Python 3.13 or newer" in combined
