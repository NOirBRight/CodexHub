#!/usr/bin/env python3
"""Prepare a source-built, platform-specific ChatGPT Web Runtime resource."""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import copy
import gzip
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

UPSTREAM_URL = "https://github.com/miuuyy/codex-chatgpt-web.git"
UPSTREAM_COMMIT = "a13cd09950969f43e3b7e25c71fa43efaf5446c5"
PATCHED_REVISION = "9c2892af646f36752cc131dedd90af6586e6e4ce"
PATCHED_TREE = "2d49d1dca11aa21a340348bb2a356c4078ef50ab"
PATCH_PATH = Path("docs/evidence/issue-590/codex-chatgpt-web-control-contract.patch")
EXPECTED_PATCH_SHA256 = "50577ded0e5b012ec7ea893f98dcd1635705470c0f093e4cb192c9cadd638c35"
PIN_PATH = Path("config/chatgpt_web_runtime_pin.json")
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(args: list[str], *, cwd: Path | None = None) -> str:
    print("+ " + " ".join(args), flush=True)
    result = subprocess.run(args, cwd=cwd, check=False, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args[0]}")
    return result.stdout.strip()


def _current_platform() -> tuple[str, str, str, str]:
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return "windows-x64", "win32", "x64", "windows-x64.zip"
    if sys.platform == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x64", "linux", "x64", "linux-x64.tar.gz"
    raise RuntimeError(f"patched ChatGPT Web Runtime packaging is unsupported on {sys.platform}/{machine}")


def _repository_pin(repo: Path) -> dict[str, Any]:
    path = repo / PIN_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("ChatGPT Web Runtime source pin is unreadable") from error
    source = document.get("runtime_source") if isinstance(document, dict) else None
    if not isinstance(source, dict):
        raise RuntimeError("ChatGPT Web Runtime source provenance is missing")
    expected = {
        "repository": UPSTREAM_URL.removesuffix(".git"),
        "commit": UPSTREAM_COMMIT,
        "patch_file": PATCH_PATH.as_posix(),
        "patch_sha256": EXPECTED_PATCH_SHA256,
        "build_revision": PATCHED_REVISION,
        "git_tree": PATCHED_TREE,
        "contract": "admin-status-v1",
    }
    if any(source.get(key) != value for key, value in expected.items()):
        raise RuntimeError("ChatGPT Web Runtime pin does not match the reviewed source and patch")
    return document


def _patch_path(repo: Path, source: dict[str, Any]) -> Path:
    path = repo / source["patch_file"]
    if not path.is_file() or _sha256(path) != source["patch_sha256"]:
        raise RuntimeError("reviewed ChatGPT Web Runtime patch is missing or changed")
    return path


def _bun_executable(repo: Path, expected_version: str) -> str:
    configured = os.environ.get("CODEXHUB_BUN_EXECUTABLE", "").strip()
    executable = configured or shutil.which("bun") or ""
    if not executable and os.name == "nt":
        candidate = repo / ".tools" / "bun.exe"
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise RuntimeError(
            f"Bun {expected_version} is required to build the patched ChatGPT Web Runtime; "
            "set CODEXHUB_BUN_EXECUTABLE to the pinned Bun executable"
        )
    reported = _run([executable, "--version"])
    if reported != expected_version:
        raise RuntimeError(f"ChatGPT Web Runtime build requires Bun {expected_version}, found {reported}")
    return executable


def _build_runtime_source(repo: Path, output: Path, source: dict[str, Any]) -> None:
    patch = _patch_path(repo, source)
    bun = _bun_executable(repo, str(source["bun_version"]))
    with tempfile.TemporaryDirectory(prefix="codexhub-chatgpt-web-source-") as temporary:
        source_dir = Path(temporary) / "source"
        source_dir.mkdir()
        _run(["git", "init", "--quiet", str(source_dir)])
        _run(["git", "-C", str(source_dir), "remote", "add", "origin", UPSTREAM_URL])
        _run(["git", "-C", str(source_dir), "fetch", "--quiet", "--depth=1", "origin", UPSTREAM_COMMIT])
        _run(["git", "-C", str(source_dir), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
        if _run(["git", "-C", str(source_dir), "rev-parse", "HEAD"]) != UPSTREAM_COMMIT:
            raise RuntimeError("upstream checkout did not match the pinned public commit")
        _run(["git", "-C", str(source_dir), "apply", "--check", str(patch)])
        _run(["git", "-C", str(source_dir), "apply", str(patch)])
        _run(["git", "-C", str(source_dir), "add", "--all"])
        tree = _run(["git", "-C", str(source_dir), "write-tree"])
        if tree != PATCHED_TREE:
            raise RuntimeError("reviewed runtime patch did not produce the pinned source tree")

        _run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], cwd=source_dir)
        launcher = source_dir / "launcher"
        _run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], cwd=launcher)
        _run([bun, "run", "typecheck"], cwd=source_dir)
        runtime_dir = source_dir / "dist" / "runtime"
        _run([bun, "run", "scripts/build-runtime-bundle.ts", str(runtime_dir)], cwd=source_dir)
        _run([bun, "run", "scripts/smoke-release.ts", str(runtime_dir)], cwd=source_dir)
        _pack_runtime(runtime_dir, output)

        with tempfile.TemporaryDirectory(prefix="codexhub-chatgpt-web-smoke-") as smoke_temp:
            unpacked = Path(smoke_temp) / "unpacked"
            _extract_runtime(output, unpacked)
            _run([bun, "run", "scripts/smoke-release.ts", str(unpacked)], cwd=source_dir)


def _pack_runtime(source: Path, destination: Path) -> None:
    key, _, _, _ = _current_platform()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    paths = sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix())
    if key == "linux-x64":
        with destination.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for path in paths:
                        relative = Path(".") / path.relative_to(source)
                        info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mtime = 0
                        info.pax_headers = {}
                        if path.is_file() and not path.is_symlink():
                            with path.open("rb") as stream:
                                archive.addfile(info, stream)
                        else:
                            archive.addfile(info)
    else:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in paths:
                if not path.is_file():
                    continue
                name = path.relative_to(source).as_posix()
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IMODE(path.stat().st_mode) & 0xFFFF) << 16
                with path.open("rb") as stream:
                    archive.writestr(info, stream.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    if destination.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError("ChatGPT Web Runtime archive exceeds the size limit")


def _extract_runtime(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                item = PurePosixPath(name.replace("\\", "/"))
                if item.is_absolute() or ".." in item.parts:
                    raise RuntimeError("ChatGPT Web Runtime archive contains an unsafe path")
            archive.extractall(destination)
        return
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            item = PurePosixPath(member.name.replace("\\", "/"))
            if item.is_absolute() or ".." in item.parts:
                raise RuntimeError("ChatGPT Web Runtime archive contains an unsafe path")
        archive.extractall(destination, filter="data")


def _read_manifest(archive_path: Path) -> dict[str, Any]:
    try:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                name = next(name for name in archive.namelist() if Path(name.replace("\\", "/")).name == "manifest.json")
                return json.loads(archive.read(name))
        with tarfile.open(archive_path, "r:*") as archive:
            member = next(member for member in archive.getmembers() if PurePosixPath(member.name).name == "manifest.json")
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError("runtime manifest is missing")
            return json.loads(stream.read())
    except (OSError, KeyError, StopIteration, tarfile.TarError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise RuntimeError("ChatGPT Web Runtime archive manifest is invalid") from error


def _validate_archive(path: Path, key: str, platform_name: str, architecture: str, expected_sha: str | None) -> str:
    if not path.is_file():
        raise RuntimeError("ChatGPT Web Runtime archive was not found")
    digest = _sha256(path)
    if expected_sha is not None and digest != expected_sha:
        raise RuntimeError("ChatGPT Web Runtime archive checksum does not match its reviewed artifact")
    manifest = _read_manifest(path)
    if (
        manifest.get("appVersion") != "6.1.1"
        or manifest.get("bunVersion") != "1.4.0"
        or manifest.get("platform") != platform_name
        or manifest.get("arch") != architecture
        or not isinstance(manifest.get("files"), list)
        or not manifest.get("files")
    ):
        raise RuntimeError(f"ChatGPT Web Runtime archive is incompatible with {key}")
    return digest


def prepare(repo: Path, resource_dir: Path, prebuilt: Path | None) -> Path:
    key, platform_name, architecture, suffix = _current_platform()
    template = _repository_pin(repo)
    source = template["runtime_source"]
    artifact_template = template["artifacts"].get(key)
    if not isinstance(artifact_template, dict):
        raise RuntimeError(f"no reviewed ChatGPT Web Runtime artifact for {key}")
    filename = artifact_template["filename"]
    if not isinstance(filename, str) or not filename.endswith(suffix):
        raise RuntimeError("pinned ChatGPT Web Runtime filename does not match this platform")

    generated = prebuilt is None
    with tempfile.TemporaryDirectory(prefix="codexhub-chatgpt-web-build-") as temporary:
        candidate = Path(temporary) / filename if generated else prebuilt.resolve()
        if generated:
            _build_runtime_source(repo, candidate, source)
        digest = _validate_archive(
            candidate,
            key,
            platform_name,
            architecture,
            None if generated else artifact_template["sha256"],
        )

        resource_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "codexhub-chatgpt-web-runtime-linux-x64.tar.gz",
            "codexhub-chatgpt-web-runtime-windows-x64.zip",
        ):
            (resource_dir / name).unlink(missing_ok=True)
        archive_path = resource_dir / filename
        shutil.copyfile(candidate, archive_path)

    pin = copy.deepcopy(template)
    pin["artifacts"] = {key: copy.deepcopy(artifact_template)}
    artifact = pin["artifacts"][key]
    artifact["sha256"] = digest
    artifact["bundled_filename"] = filename
    artifact["bundled_only"] = True
    artifact["build_revision"] = source["build_revision"]
    artifact["git_tree"] = source["git_tree"]
    artifact.pop("url", None)
    pin_file = resource_dir / "chatgpt_web_runtime_pin.json"
    pin_file.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared {key} runtime {source['build_revision']} ({digest})", flush=True)
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--resource-dir", type=Path)
    parser.add_argument("--archive", type=Path, help="use the reviewed platform artifact instead of rebuilding from source")
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    resource_dir = (args.resource_dir or repo / "src-tauri" / "resources" / "chatgpt-web-runtime").resolve()
    prebuilt = args.archive or (Path(os.environ["CODEXHUB_CHATGPT_WEB_RUNTIME_ARCHIVE"]) if os.environ.get("CODEXHUB_CHATGPT_WEB_RUNTIME_ARCHIVE") else None)
    try:
        prepare(repo, resource_dir, prebuilt)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(f"ChatGPT Web Runtime preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
