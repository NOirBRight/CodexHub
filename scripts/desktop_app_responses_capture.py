"""Check whether a sealed Codex Desktop App capture can run without UI automation.

Issue #114's primary reproducer is the Desktop App, not the bundled CLI.  This
command is deliberately fail-closed: it verifies only installed, process-local
isolation seams and refuses to substitute CLI or UI automation for a Desktop
renderer run.  Its JSON is sanitized: it emits no profile paths, credentials,
proxy addresses, request content, process IDs, or raw log lines.

If a supported renderer-driving seam is later supplied, use this readiness
report to require one disposable Electron user-data directory plus one
disposable CODEX_HOME per condition before running the fixed workload matrix.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import struct
import subprocess
from typing import Any


DESKTOP_PACKAGE_NAME = "OpenAI.Codex"
APP_RELATIVE_PATH = Path("app") / "ChatGPT.exe"
ASAR_RELATIVE_PATH = Path("app") / "resources" / "app.asar"
REQUIRED_CONDITIONS = (
    "desktop_direct_official",
    "desktop_gateway_official_auto",
    "desktop_gateway_official_explicit_proxy",
    "desktop_gateway_external_auto",
    "desktop_gateway_external_explicit_proxy",
    "cli_negative_control",
)


@dataclass(frozen=True)
class DesktopAppSeams:
    installed: bool
    build_version: str | None
    electron_user_data_override: bool
    codex_home_override: bool
    noninteractive_renderer_driver: bool


def _powershell_json(command: str) -> dict[str, Any] | None:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _installed_desktop_package() -> tuple[Path, str] | None:
    if os.name != "nt":
        return None
    package = _powershell_json(
        "Get-AppxPackage -Name OpenAI.Codex | "
        "Select-Object InstallLocation,Version | ConvertTo-Json -Compress"
    )
    if package is None:
        return None
    location = package.get("InstallLocation")
    version = package.get("Version")
    if not isinstance(location, str) or not isinstance(version, str):
        return None
    return Path(location), version


def _read_asar_file(archive: Path, relative_path: str) -> bytes:
    with archive.open("rb") as handle:
        prefix = handle.read(16)
        if len(prefix) != 16:
            raise ValueError("installed app archive header is incomplete")
        pickle_size = struct.unpack_from("<I", prefix, 4)[0]
        header_json_size = struct.unpack_from("<I", prefix, 12)[0]
        header_bytes = handle.read(header_json_size)
        if len(header_bytes) != header_json_size:
            raise ValueError("installed app archive metadata is incomplete")
        header = json.loads(header_bytes.decode("utf-8"))
        node: Any = header
        for part in relative_path.split("/"):
            files = node.get("files") if isinstance(node, dict) else None
            if not isinstance(files, dict) or part not in files:
                raise KeyError(relative_path)
            node = files[part]
        if not isinstance(node, dict):
            raise ValueError("installed app archive entry is invalid")
        offset = node.get("offset")
        size = node.get("size")
        if not isinstance(offset, str) or not isinstance(size, int):
            raise ValueError("installed app archive entry has no readable payload")
        handle.seek(8 + pickle_size + int(offset))
        payload = handle.read(size)
        if len(payload) != size:
            raise ValueError("installed app archive payload is incomplete")
        return payload


def _find_build_entry(archive: Path, prefix: str) -> str | None:
    with archive.open("rb") as handle:
        header_prefix = handle.read(16)
        if len(header_prefix) != 16:
            return None
        header_json_size = struct.unpack_from("<I", header_prefix, 12)[0]
        header_bytes = handle.read(header_json_size)
    try:
        header = json.loads(header_bytes.decode("utf-8"))
        files = header["files"][".vite"]["files"]["build"]["files"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(files, dict):
        return None
    return next((f".vite/build/{name}" for name in files if name.startswith(prefix)), None)


def _desktop_app_seams() -> DesktopAppSeams:
    package = _installed_desktop_package()
    if package is None:
        return DesktopAppSeams(False, None, False, False, False)
    install_root, version = package
    executable = install_root / APP_RELATIVE_PATH
    archive = install_root / ASAR_RELATIVE_PATH
    if not executable.is_file() or not archive.is_file():
        return DesktopAppSeams(False, version, False, False, False)
    try:
        bootstrap_path = _find_build_entry(archive, "bootstrap-")
        source_path = _find_build_entry(archive, "src-")
        bootstrap = _read_asar_file(archive, bootstrap_path).decode("utf-8") if bootstrap_path else ""
        source = _read_asar_file(archive, source_path).decode("utf-8") if source_path else ""
    except (KeyError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return DesktopAppSeams(True, version, False, False, False)
    # The installed build's bootstrap recognizes this before it sets Electron's
    # userData path.  The app-server launcher resolves CODEX_HOME from its
    # inherited process environment.  No supported renderer-driving API is
    # exposed through the current task tools or the installed package.
    return DesktopAppSeams(
        installed=True,
        build_version=version,
        electron_user_data_override="CODEX_ELECTRON_USER_DATA_PATH" in bootstrap,
        codex_home_override="process.env.CODEX_HOME" in source,
        noninteractive_renderer_driver=False,
    )


def readiness_report() -> dict[str, Any]:
    seams = _desktop_app_seams()
    ready_for_safe_launch = seams.installed and seams.electron_user_data_override and seams.codex_home_override
    return {
        "surface": "desktop_app_primary",
        "status": "blocked_missing_noninteractive_renderer_driver",
        "desktop_app": asdict(seams),
        "safe_isolation_launch_ready": ready_for_safe_launch,
        "required_matrix": list(REQUIRED_CONDITIONS),
        "cli_role": "contemporaneous_negative_control_only",
        "capture_contract": {
            "must_correlate": [
                "app_build_rollout_and_app_server_lifecycle",
                "client_stream_consumer_or_continuation_boundary",
                "sanitized_request_process_and_connection_labels",
                "gateway_failure_phase_and_terminal_outcome",
                "whether_the_app_or_gateway_closed_first",
            ],
            "must_not": [
                "mutate_shared_desktop_state",
                "mutate_global_proxy_or_credentials",
                "use_computer_use_or_cli_as_desktop_substitute",
                "emit_raw_logs_or_local_paths",
            ],
        },
        "exact_required_reporter_action": (
            "Provide a supported noninteractive Desktop-App renderer hook or official deep-link/API that can "
            "target the disposable Electron profile, submit one fixed Responses turn, and expose the renderer "
            "stream-consumer lifecycle. The hook must permit the five Desktop conditions above plus the CLI control "
            "without using UI automation or the shared Desktop profile."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = readiness_report()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    # A blocked result is intentional: it prevents a false Desktop qualification.
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
