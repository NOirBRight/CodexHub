"""Shared native-Windows fixture helpers for ChatGPT Web tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
from pathlib import Path


def fixture_python(env: dict[str, str]) -> str:
    """Return the interpreter and scoped environment used by Windows fixtures."""
    python = sys.executable
    if os.name == "nt":
        python = sys._base_executable
        env["PYTHONHOME"] = sys.base_prefix
        env["PATH"] = os.pathsep.join((str(Path(python).parent), env.get("PATH", "")))
    return python


def write_windows_python_launcher(
    executable: Path,
    payload_source: str,
    *,
    payload_name: str = "fixture_payload.py",
) -> Path:
    """Create a real PE launcher and its adjacent Python fixture payload."""
    executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sys._base_executable, executable)
    payload = executable.with_name(payload_name)
    payload.write_text(payload_source, encoding="utf-8")
    return payload


def write_windows_runtime_archive(
    directory: Path,
    entry_name: str,
    payload_source: str,
    *,
    archive_name: str = "runtime.tar.gz",
) -> Path:
    """Package a PE runtime launcher, adjacent payload, and launcher manifest."""
    tree = directory / "tree"
    entry = tree / Path(entry_name)
    payload = write_windows_python_launcher(entry, payload_source)
    manifest = entry.parent.parent / "manifest.json"
    manifest.write_text(
        json.dumps({"launcher": Path(entry_name).as_posix()}), encoding="utf-8"
    )
    archive = directory / archive_name
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(entry, arcname=Path(entry_name).as_posix())
        bundle.add(payload, arcname=payload.relative_to(tree).as_posix())
        bundle.add(manifest, arcname="manifest.json")
    return archive


def prepare_windows_runtime_trampolines(
    home: Path,
    entry_name: str,
    commands: tuple[str, ...] = ("doctor", "serve", "login"),
) -> None:
    """Create command files that load the payload beside the active launcher."""
    if os.name != "nt":
        return
    entry = home / "current" / "runtime" / Path(entry_name)
    if not entry.with_name("fixture_payload.py").is_file():
        return
    web_home = home / "web-home"
    web_home.mkdir(parents=True, exist_ok=True)
    trampoline = '''import os
import runpy
import sys
from pathlib import Path

payload = Path(os.environ["CODEX_CHATGPT_WEB_LAUNCHER"]).with_name("fixture_payload.py")
sys.argv.insert(1, sys.argv[0])
sys.argv[0] = str(payload)
runpy.run_path(str(payload), run_name="__main__")
'''
    for command in commands:
        path = web_home / command
        if not path.is_file() or path.read_text(encoding="utf-8") != trampoline:
            path.write_text(trampoline, encoding="utf-8")
