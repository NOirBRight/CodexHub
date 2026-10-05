"""Exercise the real Windows runtime preparer in a disposable source repository."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "src-tauri/resources/python/.keep"


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, encoding="utf-8", check=True, timeout=10)
    return result.stdout.strip()


def test_preparer_preserves_exact_tracked_source_through_actual_runtime_lifecycle(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows embeddable runtime preparation")
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows PowerShell 5.1 is required"
    # These are the real pinned archives cached by canonical preparation, not
    # packaging stand-ins. Missing preparation is a failure on Windows.
    cache = ROOT / "src-tauri/resources/downloads"
    python_zip = cache / "python-3.13.14-embed-amd64.zip"
    wheel = cache / "zstandard-0.25.0-cp313-cp313-win_amd64.whl"
    assert python_zip.is_file() and wheel.is_file(), "Run Prepare-PythonRuntime.ps1 before Windows checks"
    source = tmp_path / "source"
    runtime = source / "src-tauri/resources/python"
    downloads = source / "src-tauri/resources/downloads"
    runtime.mkdir(parents=True)
    downloads.mkdir()
    (source / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/Prepare-PythonRuntime.ps1", source / "scripts")
    shutil.copytree(ROOT / "src-python", source / "src-python", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "config", source / "config")
    placeholder = source / PLACEHOLDER
    expected_bytes = (ROOT / PLACEHOLDER).read_bytes()
    placeholder.write_bytes(expected_bytes)
    (source / ".gitignore").write_text(
        "__pycache__/\nsrc-tauri/resources/python/*\n!src-tauri/resources/python/.keep\nsrc-tauri/resources/downloads/\n",
        encoding="utf-8")
    git(source, "init", "--quiet")
    git(source, "config", "core.autocrlf", "true")
    git(source, "add", ".")
    git(source, "-c", "user.name=Runtime fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "Disposable runtime source")
    expected_blob = git(ROOT, "rev-parse", "HEAD:" + PLACEHOLDER)
    assert git(source, "rev-parse", "HEAD:" + PLACEHOLDER) == expected_blob
    source_head, source_tree = git(source, "rev-parse", "HEAD"), git(source, "rev-parse", "HEAD^{tree}")

    def assert_source():
        assert placeholder.read_bytes() == expected_bytes
        assert git(source, "hash-object", "--path=" + PLACEHOLDER, PLACEHOLDER) == expected_blob
        assert git(source, "status", "--porcelain=v1", "--untracked-files=no") == ""
        assert git(source, "rev-parse", "HEAD") == source_head
        assert git(source, "rev-parse", "HEAD^{tree}") == source_tree

    def seed_cache():
        shutil.copy2(python_zip, downloads / python_zip.name)
        shutil.copy2(wheel, downloads / wheel.name)

    def prepare(*args, succeeds=True):
        env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path),
                   APPDATA=str(tmp_path / "appdata"), LOCALAPPDATA=str(tmp_path / "localappdata"))
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(source / "scripts/Prepare-PythonRuntime.ps1"),
                                 "-RepoRoot", str(source), *args], cwd=source,
                                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
        assert_source()
        return result.stdout + result.stderr

    assert_source()  # Tracked source is clean before any preparation or tests.
    seed_cache()
    assert "Python runtime check failed" in prepare("-CheckOnly", succeeds=False)
    assert set(path.name for path in runtime.iterdir()) == {".keep"}
    assert "Python runtime prepared" in prepare()  # Fresh real installation.
    assert (runtime / "python.exe").is_file()
    manifest = (runtime / "codexhub-python-runtime.json").read_bytes()
    assert "Python runtime already prepared" in prepare()  # No-op.
    assert (runtime / "codexhub-python-runtime.json").read_bytes() == manifest
    assert "Python runtime check passed" in prepare("-CheckOnly")
    stale = runtime / "stale-binary.dll"
    stale.write_bytes(b"stale fixture binary")
    assert "Python runtime prepared" in prepare("-Force")
    assert not stale.exists()
    assert "Python runtime check passed" in prepare("-CheckOnly")

    # Hash rejection after forced cleanup must leave only the tracked file.
    output = prepare("-Force", "-PythonZipSha256", "0" * 64,
                     "-PythonZipUrl", python_zip.as_uri(), succeeds=False)
    assert "Python runtime hash mismatch" in output
    assert set(path.name for path in runtime.iterdir()) == {".keep"}
    assert "Python runtime check failed" in prepare("-CheckOnly", succeeds=False)

    # An intentionally incomplete ZIP exercises failure *after* extraction.
    # It cannot pass readiness and is never used as a packaged runtime.
    broken = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("incomplete-runtime.dll", b"not an installable runtime")
    seed_cache()
    shutil.copy2(broken, downloads / python_zip.name)
    output = prepare("-Force", "-PythonZipSha256", hashlib.sha256(broken.read_bytes()).hexdigest(), succeeds=False)
    assert "embeddable ._pth file was not found" in output
    assert set(path.name for path in runtime.iterdir()) == {".keep"}
    assert not list(downloads.glob("python-extract-*"))
    assert "Python runtime check failed" in prepare("-CheckOnly", succeeds=False)
    seed_cache()
    assert "Python runtime prepared" in prepare()  # Recover after failure.
    assert "Python runtime check passed" in prepare("-CheckOnly")
