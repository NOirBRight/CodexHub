from __future__ import annotations

import os
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


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is required for the repository Python launcher")
    return executable


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
    result = subprocess.run(
        [str(CMD_LAUNCHER), "-c", "import os, shutil, sys; path=os.environ['PATH'].split(os.pathsep); print(sys.executable); print(shutil.which('python')); print(shutil.which('pytest')); print(path[0]); print(path[1])"],
        cwd=ROOT,
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


def test_repository_selector_rejects_an_explicit_python_311_override() -> None:
    ambient = shutil.which("python")
    if ambient is None:
        pytest.skip("ambient Python executable is unavailable")
    version = subprocess.check_output(
        [ambient, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()
    major, minor = (int(part) for part in version.split(".", 1))
    if (major, minor) >= (3, 13):
        pytest.skip("ambient Python is already compatible")

    result = _run_script(SELECTOR, "-PrintPath", env={"CODEXHUB_PYTHON": ambient})
    assert result.returncode != 0
    assert "explicit interpreter is not compatible" in result.stdout + result.stderr


def test_repository_selector_rejects_an_incompatible_proxy_override_without_fallback() -> None:
    ambient = shutil.which("python")
    if ambient is None:
        pytest.skip("ambient Python executable is unavailable")
    version = subprocess.check_output(
        [ambient, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()
    major, minor = (int(part) for part in version.split(".", 1))
    if (major, minor) >= (3, 13):
        pytest.skip("ambient Python is already compatible")

    result = _run_script(
        SELECTOR,
        "-PrintPath",
        env={"CODEXHUB_PYTHON": "", "CODEXHUB_PROXY_PYTHON": ambient},
    )
    assert result.returncode != 0
    assert "explicit interpreter is not compatible" in result.stdout + result.stderr


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


def test_pytest_preflight_rejects_python_311_before_source_collection() -> None:
    ambient = shutil.which("python")
    if ambient is None:
        pytest.skip("ambient Python executable is unavailable")
    version = subprocess.check_output(
        [ambient, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()
    major, minor = (int(part) for part in version.split(".", 1))
    if (major, minor) >= (3, 13):
        pytest.skip("ambient Python is already compatible")

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
    ambient = shutil.which("python")
    if ambient is None:
        pytest.skip("ambient Python executable is unavailable")
    version = subprocess.check_output(
        [ambient, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        text=True,
    ).strip()
    major, minor = (int(part) for part in version.split(".", 1))
    if (major, minor) >= (3, 13):
        pytest.skip("ambient Python is already compatible")

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
