"""Run a bounded, human-assisted Desktop App capture for Issue #114.

This runner deliberately has no renderer driver.  It creates one disposable
Desktop profile, launches the installed Desktop App only when a maintainer
explicitly asks it to, and collects sanitized lifecycle/Gateway evidence after
the maintainer finishes the visible turn.  It never reads or copies a shared
profile, credential, proxy configuration, or request body.

The active localization leg inserts an isolated Gateway into the known-fault
Official Desktop path while retaining the same Desktop build/profile/model and
automatic Windows proxy discovery.  Direct Official stability is retained only
as historical reporter context, not rerun as a rate control.  Gateway retries
are disabled only to expose the first failure boundary; WebSockets remain
disabled for the Gateway condition.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src-python"
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
for _root in (str(SOURCE_ROOT), str(SCRIPTS_ROOT)):
    if _root not in sys.path:
        sys.path.insert(0, _root)

import config_overlay
from desktop_app_responses_capture import APP_RELATIVE_PATH, _desktop_app_seams, _installed_desktop_package
from e2e_codex_catalog_roundtrip import resolve_app_codex


DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_PROMPT_BYTES = 0
SESSION_PREFIX = "codexhub-issue-114-manual"
SESSION_ID_RE = re.compile(r"^[a-z0-9]{12}$")
AUTH_BOOTSTRAP_LEG = "auth_bootstrap"
LEGS = (AUTH_BOOTSTRAP_LEG, "direct_official", "gateway_official_auto")
APP_RESULTS = (
    "completed",
    "reconnecting",
    "stream_disconnected",
    "duplicate_request_visible",
    "timed_out",
    "auth_required",
    "aborted",
)
LONG_STREAM_PROTOCOL = "desktop_direct_official_long_stream_v1"
LONG_STREAM_PROMPT_IDENTIFIER = "direct_official_reliability_64x80_90_v1"
LONG_STREAM_PROMPT_SHA256 = "5e1a8777d2b410a11485b50b957342fbe2e228a80bc415712e1b6a2454b521a2"
LONG_STREAM_TARGET_DURATION_MS = 45_000
LONG_STREAM_MARKERS = ("first_visible_output", "stream_active_target_reached")
LONG_STREAM_FIRST_VISIBLE_SEMANTICS = "operator_observed_renderer_proxy_not_transport_first_byte"
PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
LAUNCH_READINESS_STRATEGIES = (
    "baseline",
    "explicit_user_data_arg",
    "empty_codex_home",
    "disable_gpu",
    "electron_logging",
)
DEFAULT_LAUNCHER_STRATEGY = "explicit_user_data_arg"
DEFAULT_LAUNCH_READINESS_SECONDS = 8.0
MAX_LAUNCH_DIAGNOSTIC_BYTES = 4096
BOUNDARY_TRACE_SCHEMA_VERSION = 2
BOUNDARY_CLOSING_SIDES = (
    "downstream_client",
    "gateway_relay",
    "upstream_transport",
    "upstream_service",
    "unknown",
)
BOUNDARY_FAILURE_PHASES = (
    "dns",
    "tcp_connect",
    "tls_handshake",
    "request_write",
    "response_headers",
    "sse_read",
    "downstream_body",
    "downstream_write",
    "relay_terminalization",
    "unknown",
)
GATEWAY_EVENT_FIELDS = (
    "status",
    "failure_phase",
    "failure_side",
    "failure_class",
    "client_disconnected",
    "lines_streamed",
    "bytes_streamed",
    "sse_events_streamed",
    "sse_terminal_event_seen",
    "sse_completed_event_seen",
    "sse_downstream_output_seen",
    "synthetic_terminal_event_sent",
    "synthetic_terminal_event_type",
    "attempt",
    "max_attempts",
    "delay_ms",
    "duration_ms",
    "last_upstream_byte_age_ms",
    "headers_sent_downstream",
    "downstream_sse_started",
    "error",
)


class CaptureError(RuntimeError):
    """A safe, user-actionable capture error with no path or secret payload."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _session_root(session_id: str) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id):
        raise CaptureError("invalid_session_id")
    return Path(tempfile.gettempdir()) / SESSION_PREFIX / session_id


def _state_path(root: Path) -> Path:
    return root / "state.json"


def _capture_path(root: Path, name: str) -> Path:
    return root / "capture" / name


def _load_state(session_id: str) -> dict[str, Any]:
    try:
        value = json.loads(_state_path(_session_root(session_id)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureError("unknown_or_corrupt_session") from exc
    if not isinstance(value, dict) or value.get("session_id") != session_id:
        raise CaptureError("unknown_or_corrupt_session")
    return value


def _write_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_jsonl(path: Path, payload: dict[str, Any], *, lock: threading.Lock | None = None) -> None:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if lock is None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(rendered)
        return
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(rendered)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    result.append(value)
    except OSError:
        return []
    return result


def _opaque_label(state: dict[str, Any], category: str, value: object) -> str:
    secret = state.get("session_hmac_key")
    if not isinstance(secret, str):
        return f"{category}-unavailable"
    digest = hmac.new(
        secret.encode("ascii"),
        f"{category}\0{value}".encode("utf-8", "replace"),
        hashlib.sha256,
    ).hexdigest()[:16]
    return f"{category}-{digest}"


def _runtime_paths(root: Path) -> dict[str, Path]:
    return {
        "codex_home": root / "codex-home",
        "electron_user_data": root / "electron-user-data",
        "catalog": root / "codex-home" / "model-catalogs" / "codexhub-model-catalog.json",
        "config": root / "codex-home" / "config.toml",
        "overlay_backup": root / "codex-home" / "proxy" / "manual-capture-config.backup.toml",
        "gateway_events": root / "codex-home" / "proxy" / "codex-proxy-events.jsonl",
        "watch_stop": root / "capture" / "stop-watcher",
    }


def _manual_child_environment(root: Path, *, gateway_key: str | None = None) -> dict[str, str]:
    paths = _runtime_paths(root)
    environment = os.environ.copy()
    for name in PROXY_ENV_NAMES:
        environment.pop(name, None)
    environment["CODEX_HOME"] = str(paths["codex_home"])
    environment["CODEX_ELECTRON_USER_DATA_PATH"] = str(paths["electron_user_data"])
    environment["CODEX_PROXY_AUTO_RETRY_ENABLED"] = "0"
    if gateway_key:
        environment["CODEX_PROXY_GATEWAY_CLIENT_KEY"] = gateway_key
    else:
        environment.pop("CODEX_PROXY_GATEWAY_CLIENT_KEY", None)
    return environment


def _desktop_command(executable: Path, root: Path, launcher_strategy: str) -> list[str]:
    """Build one isolated Desktop command, changing one variable per strategy."""

    if launcher_strategy not in LAUNCH_READINESS_STRATEGIES:
        raise CaptureError("unsupported_launcher_strategy")
    command = [str(executable)]
    paths = _runtime_paths(root)
    if launcher_strategy == "explicit_user_data_arg":
        command.append(f"--user-data-dir={paths['electron_user_data']}")
    elif launcher_strategy == "disable_gpu":
        command.append("--disable-gpu")
    elif launcher_strategy == "electron_logging":
        command.append("--enable-logging=stderr")
    return command


def _launcher_probe_environment(root: Path, launcher_strategy: str) -> dict[str, str]:
    """Return a disposable environment for a launch-readiness probe."""

    environment = _manual_child_environment(root)
    if launcher_strategy == "empty_codex_home":
        # The empty directory is still isolated; only the baseline config file
        # changes so this tests whether the minimal CODEX_HOME config is causal.
        _runtime_paths(root)["config"].unlink(missing_ok=True)
    return environment


def _new_diagnostic_sink() -> dict[str, Any]:
    return {"bytes": 0, "prefix": bytearray()}


def _drain_diagnostic_stream(stream: Any, sink: dict[str, Any]) -> None:
    """Drain a process pipe without persisting raw Desktop diagnostics."""

    try:
        while True:
            chunk = stream.read(1024)
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", "replace")
            sink["bytes"] = int(sink["bytes"]) + len(chunk)
            prefix = sink["prefix"]
            if isinstance(prefix, bytearray) and len(prefix) < MAX_LAUNCH_DIAGNOSTIC_BYTES:
                remaining = MAX_LAUNCH_DIAGNOSTIC_BYTES - len(prefix)
                prefix.extend(chunk[:remaining])
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _start_diagnostic_drains(process: subprocess.Popen[bytes]) -> tuple[dict[str, Any], dict[str, Any], list[threading.Thread]]:
    stdout = _new_diagnostic_sink()
    stderr = _new_diagnostic_sink()
    threads: list[threading.Thread] = []
    for stream, sink, name in ((process.stdout, stdout, "stdout"), (process.stderr, stderr, "stderr")):
        if stream is None:
            continue
        thread = threading.Thread(target=_drain_diagnostic_stream, args=(stream, sink), name=f"launcher-{name}", daemon=True)
        thread.start()
        threads.append(thread)
    return stdout, stderr, threads


def _launcher_diagnostic_summary(sink: dict[str, Any]) -> dict[str, int | str]:
    """Classify a bounded diagnostic prefix and discard it before reporting."""

    prefix = sink.get("prefix")
    rendered = bytes(prefix).decode("utf-8", "replace").casefold() if isinstance(prefix, bytearray) else ""
    categories = (
        ("permission_denied", ("access is denied", "permission denied", "eacces")),
        ("missing_dependency", ("module not found", "could not load", "dll", "enoent")),
        ("sandbox_or_gpu", ("sandbox", "gpu", "angle", "d3d", "direct3d")),
        ("packaged_activation", ("appmodel", "package activation", "appx")),
    )
    category = "empty" if not rendered else "nonempty_unclassified"
    for candidate, needles in categories:
        if any(needle in rendered for needle in needles):
            category = candidate
            break
    if isinstance(prefix, bytearray):
        prefix.clear()
    return {"bytes": int(sink.get("bytes", 0)), "category": category}


def _process_has_main_window(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    command = (
        f"$process = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
        "if ($null -ne $process -and $process.MainWindowHandle -ne 0) { exit 0 } else { exit 1 }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.returncode == 0


def _desktop_process_snapshot(root_pid: int) -> dict[str, bool | int]:
    """Observe only lifecycle aggregates for the exact launch tree."""

    known: dict[int, str] = {root_pid: "desktop"}
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        for child_pid, name in _process_children(parent_pid):
            if child_pid in known:
                continue
            folded = name.casefold()
            role = "app_server" if "codex" in folded else "desktop_child" if "chatgpt" in folded else "helper"
            known[child_pid] = role
            pending.append(child_pid)
    live = {pid for pid in known if _pid_alive(pid)}
    return {
        "root_observed": root_pid in live,
        "tree_alive": bool(live),
        "main_window_seen": any(_process_has_main_window(pid) for pid in live),
        "app_server_seen": any(known[pid] == "app_server" for pid in live),
        "desktop_child_count": sum(1 for pid in live if known[pid] == "desktop_child"),
        "helper_count": sum(1 for pid in live if known[pid] == "helper"),
    }


def _await_desktop_launch_readiness(
    process: subprocess.Popen[Any], duration_seconds: float = DEFAULT_LAUNCH_READINESS_SECONDS
) -> dict[str, bool | int | str | None]:
    """Wait for a window or app-server, without driving the Desktop UI."""

    deadline = time.monotonic() + duration_seconds
    root_observed = False
    while time.monotonic() < deadline:
        snapshot = _desktop_process_snapshot(process.pid)
        root_observed = root_observed or bool(snapshot["root_observed"])
        if bool(snapshot["main_window_seen"]):
            return {
                "status": "ready",
                "readiness_indicator": "main_window",
                "root_observed": root_observed,
                "exit_code": process.poll(),
            }
        if bool(snapshot["app_server_seen"]):
            return {
                "status": "ready",
                "readiness_indicator": "app_server",
                "root_observed": root_observed,
                "exit_code": process.poll(),
            }
        if not bool(snapshot["tree_alive"]):
            return {
                "status": "not_ready",
                "classification": "process_tree_ended_before_readiness",
                "root_observed": root_observed,
                "exit_code": process.poll(),
            }
        time.sleep(0.25)
    return {
        "status": "not_ready",
        "classification": "alive_without_window_or_app_server",
        "root_observed": root_observed,
        "exit_code": process.poll(),
    }


def _remove_disposable_launcher_profile(root: Path) -> str:
    """Delete only the temp profile allocated by this probe invocation."""

    temporary_parent = Path(tempfile.gettempdir()).resolve()
    resolved_root = root.resolve()
    if resolved_root.parent != temporary_parent or not resolved_root.name.startswith(f"{SESSION_PREFIX}-launcher-"):
        return "removal_skipped_unexpected_location"
    try:
        shutil.rmtree(resolved_root)
    except OSError:
        return "removal_failed"
    return "removed_disposable_profile"


def _stop_owned_desktop_tree(root_pid: int) -> str:
    """Stop only the exact freshly-launched ChatGPT tree after identity checking."""

    if not _pid_alive(root_pid):
        return "not_needed"
    if os.name != "nt":
        return "unsupported_platform"
    command = (
        f"$process = Get-Process -Id {root_pid} -ErrorAction SilentlyContinue; "
        "if ($null -eq $process -or $process.ProcessName -ne 'ChatGPT') { exit 3 }; "
        f"& taskkill.exe /PID {root_pid} /T /F | Out-Null; "
        "$deadline = [DateTime]::UtcNow.AddSeconds(5); "
        f"while ((Get-Process -Id {root_pid} -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) "
        "{ Start-Sleep -Milliseconds 100 }; "
        f"if (Get-Process -Id {root_pid} -ErrorAction SilentlyContinue) {{ exit 2 }}; exit 0"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode == 0:
        return "stopped_task_owned_tree"
    if completed.returncode == 3:
        return "cleanup_skipped_identity_mismatch"
    return "cleanup_failed"


def _launch_readiness_probe(launcher_strategy: str, duration_seconds: float) -> dict[str, Any]:
    """Run one clean, unattended isolated Desktop start/stop observation."""

    if launcher_strategy not in LAUNCH_READINESS_STRATEGIES:
        raise CaptureError("unsupported_launcher_strategy")
    install_root, version = _require_desktop_seams()
    root = Path(tempfile.mkdtemp(prefix=f"{SESSION_PREFIX}-launcher-"))
    paths = _runtime_paths(root)
    paths["codex_home"].mkdir(parents=True)
    paths["electron_user_data"].mkdir(parents=True)
    paths["config"].write_text(_base_config(DEFAULT_MODEL), encoding="utf-8")
    executable = install_root / APP_RELATIVE_PATH
    process: subprocess.Popen[bytes] | None = None
    cleanup = "not_needed"
    storage_cleanup = "not_attempted"
    result: dict[str, Any] = {}
    try:
        environment = _launcher_probe_environment(root, launcher_strategy)
        try:
            process = subprocess.Popen(
                _desktop_command(executable, root, launcher_strategy),
                cwd=executable.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            result = {
                "strategy": launcher_strategy,
                "status": "not_ready",
                "classification": "launch_os_error",
                "error_category": type(exc).__name__,
                "desktop_build_version": version,
                "shared_state_touched": False,
            }
        else:
            stdout, stderr, drains = _start_diagnostic_drains(process)
            deadline = time.monotonic() + duration_seconds
            root_observed = False
            main_window_seen = False
            app_server_seen = False
            tree_ended_before_deadline = False
            indicator = "none"
            while time.monotonic() < deadline:
                snapshot = _desktop_process_snapshot(process.pid)
                root_observed = root_observed or bool(snapshot["root_observed"])
                main_window_seen = main_window_seen or bool(snapshot["main_window_seen"])
                app_server_seen = app_server_seen or bool(snapshot["app_server_seen"])
                if main_window_seen:
                    indicator = "main_window"
                elif app_server_seen:
                    indicator = "app_server"
                if not bool(snapshot["tree_alive"]):
                    tree_ended_before_deadline = True
                    break
                time.sleep(0.25)
            exit_code = process.poll()
            survived_observation_window = not tree_ended_before_deadline
            if main_window_seen or app_server_seen:
                status = "ready"
                classification = "isolated_lifecycle_ready"
            elif tree_ended_before_deadline:
                status = "not_ready"
                classification = "process_tree_ended_before_readiness"
            else:
                status = "not_ready"
                classification = "alive_without_window_or_app_server"
            cleanup = _stop_owned_desktop_tree(process.pid)
            for thread in drains:
                thread.join(timeout=2)
            result = {
                "strategy": launcher_strategy,
                "status": status,
                "classification": classification,
                "desktop_build_version": version,
                "exit_code_before_cleanup": exit_code,
                "lifecycle": {
                    "root_observed": root_observed,
                    "main_window_seen": main_window_seen,
                    "app_server_seen": app_server_seen,
                    "readiness_indicator": indicator,
                    "tree_ended_before_deadline": tree_ended_before_deadline,
                    "survived_observation_window": survived_observation_window,
                },
                "diagnostics": {
                    "stdout": _launcher_diagnostic_summary(stdout),
                    "stderr": _launcher_diagnostic_summary(stderr),
                },
                "cleanup": cleanup,
                "shared_state_touched": False,
            }
    finally:
        if process is not None and _pid_alive(process.pid):
            cleanup = _stop_owned_desktop_tree(process.pid)
        if cleanup in {"not_needed", "stopped_task_owned_tree"}:
            storage_cleanup = _remove_disposable_launcher_profile(root)
        else:
            storage_cleanup = "preserved_for_safe_cleanup"
        if result:
            result["disposable_profile_cleanup"] = storage_cleanup
            result["cleanup"] = cleanup
    return result


def launch_readiness(launcher_strategy: str = "all", duration_seconds: float = DEFAULT_LAUNCH_READINESS_SECONDS) -> dict[str, Any]:
    """Exercise the bounded launch hypotheses without touching a human session."""

    if launcher_strategy != "all" and launcher_strategy not in LAUNCH_READINESS_STRATEGIES:
        raise CaptureError("unsupported_launcher_strategy")
    if not 3.0 <= duration_seconds <= 20.0:
        raise CaptureError("launch_readiness_duration_out_of_range")
    strategies = LAUNCH_READINESS_STRATEGIES if launcher_strategy == "all" else (launcher_strategy,)
    results = [_launch_readiness_probe(strategy, duration_seconds) for strategy in strategies]
    ready = [item["strategy"] for item in results if item.get("status") == "ready"]
    return {
        "status": "ready" if ready else "not_ready",
        "probe_count": len(results),
        "recommended_launcher_strategy": ready[0] if ready else None,
        "results": results,
        "guardrails": [
            "fresh_disposable_profile_per_probe",
            "no_shared_profile_credentials_or_database",
            "no_shell_activation_or_global_proxy_changes",
            "only_identity_checked_task_owned_process_tree_is_stopped",
        ],
    }


def _manual_reserve_loopback_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.returncode == 0
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _classify_isolated_background_desktop(root: Path, state: dict[str, Any]) -> dict[str, bool | str]:
    """Classify a surviving task-owned Desktop process without controlling it.

    A normal ``CloseMainWindow`` can leave the packaged Desktop App alive with
    no visible window.  Collection may record that narrow, identity-verified
    state, but it must never terminate or otherwise control the process.
    """

    pid = state.get("current_app_pid")
    result: dict[str, bool | str] = {
        "state": "unverified_running_process",
        "identity_verified": False,
        "visible_main_window": False,
        "responsive": False,
    }
    if not isinstance(pid, int) or pid <= 0 or not _pid_alive(pid):
        result["state"] = "not_running"
        return result
    if os.name != "nt":
        return result

    environment = os.environ.copy()
    environment["CODEXHUB_EXPECTED_USER_DATA"] = str(_runtime_paths(root)["electron_user_data"])
    command = "\n".join(
        (
            f"$process = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction SilentlyContinue",
            f"$runtime = Get-Process -Id {pid} -ErrorAction SilentlyContinue",
            "if ($null -eq $process -or $null -eq $runtime) { @{present=$false} | ConvertTo-Json -Compress; exit 0 }",
            "$expected = [Environment]::GetEnvironmentVariable('CODEXHUB_EXPECTED_USER_DATA')",
            "$commandLine = [string]$process.CommandLine",
            "$hasUserDataArgument = $commandLine -match '(?i)(?:^|\\s)--user-data-dir(?:=|\\s)'",
            "$matchesIsolatedUserData = $commandLine.IndexOf($expected, [System.StringComparison]::OrdinalIgnoreCase) -ge 0",
            "@{",
            "  present=$true;",
            "  name_is_expected=([string]$process.Name -eq 'ChatGPT.exe');",
            "  has_user_data_argument=$hasUserDataArgument;",
            "  matches_isolated_user_data=$matchesIsolatedUserData;",
            "  visible_main_window=([int64]$runtime.MainWindowHandle -ne 0);",
            "  responsive=[bool]$runtime.Responding",
            "} | ConvertTo-Json -Compress",
        )
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict) or not payload.get("present"):
        result["state"] = "not_running" if isinstance(payload, dict) and payload.get("present") is False else "unverified_running_process"
        return result

    identity_verified = all(
        payload.get(key) is True
        for key in ("name_is_expected", "has_user_data_argument", "matches_isolated_user_data")
    )
    visible_main_window = payload.get("visible_main_window") is True
    responsive = payload.get("responsive") is True
    result.update(
        {
            "identity_verified": identity_verified,
            "visible_main_window": visible_main_window,
            "responsive": responsive,
        }
    )
    if identity_verified and not visible_main_window and responsive:
        result["state"] = "background_after_normal_close"
    elif identity_verified and visible_main_window:
        result["state"] = "visible_window_still_open"
    elif identity_verified:
        result["state"] = "task_owned_process_still_running"
    return result


def _manual_wait_for_gateway(port: int, timeout_seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError):
            time.sleep(0.1)
    return False


def _base_config(model: str) -> str:
    return "\n".join(
        (
            f'model = "{model}"',
            'model_provider = "openai"',
            "",
            "[features]",
            "responses_websockets = false",
            "responses_websockets_v2 = false",
            "",
        )
    )


def _require_desktop_seams() -> tuple[Path, str]:
    seams = _desktop_app_seams()
    if not (seams.installed and seams.electron_user_data_override and seams.codex_home_override):
        raise CaptureError("desktop_isolation_seam_unavailable")
    package = _installed_desktop_package()
    if package is None:
        raise CaptureError("desktop_app_not_installed")
    install_root, version = package
    if not (install_root / APP_RELATIVE_PATH).is_file():
        raise CaptureError("desktop_executable_unavailable")
    return install_root, version


def _prepare_catalog(root: Path, state: dict[str, Any]) -> None:
    paths = _runtime_paths(root)
    completed = subprocess.run(
        [sys.executable, "-c", "from catalog_sync import sync_catalog; sync_catalog()"],
        cwd=SOURCE_ROOT,
        env=_manual_child_environment(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0 or not paths["catalog"].is_file():
        raise CaptureError("isolated_catalog_generation_failed")
    state["catalog_generated"] = True


def _start_gateway(root: Path, state: dict[str, Any]) -> None:
    if _pid_alive(state.get("gateway_pid")):
        return
    _prepare_catalog(root, state)
    paths = _runtime_paths(root)
    gateway_key = secrets.token_urlsafe(24)
    port = _manual_reserve_loopback_port()
    try:
        config_overlay.apply_overlay(
            paths["config"],
            paths["overlay_backup"],
            paths["catalog"],
            f"http://127.0.0.1:{port}",
            owner="beta",
            gateway_key=gateway_key,
        )
    except (OSError, ValueError) as exc:
        raise CaptureError("isolated_gateway_overlay_failed") from exc
    state.update(
        {
            "gateway_key": gateway_key,
            "gateway_port": port,
            "gateway_overlay_active": True,
            "gateway_proxy_mode": "auto_windows_registry",
        }
    )
    _write_state(root, state)
    environment = _manual_child_environment(root, gateway_key=gateway_key)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_gateway", "--session", str(state["session_id"])],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=creationflags,
    )
    state["gateway_pid"] = process.pid
    _write_state(root, state)
    if not _manual_wait_for_gateway(port):
        _append_jsonl(
            _capture_path(root, "gateway-trace.jsonl"),
            {"at": _utc_now(), "event": "gateway_start_failed"},
        )
        raise CaptureError("isolated_gateway_start_failed")
    _append_jsonl(
        _capture_path(root, "gateway-trace.jsonl"),
        {"at": _utc_now(), "event": "gateway_ready", "proxy_mode": "auto_windows_registry"},
    )


def _launch_desktop(root: Path, state: dict[str, Any], leg: str, launcher_strategy: str) -> dict[str, bool | int | str | None]:
    if _pid_alive(state.get("current_app_pid")):
        raise CaptureError("previous_isolated_desktop_instance_is_still_running")
    install_root, version = _require_desktop_seams()
    executable = install_root / APP_RELATIVE_PATH
    environment = _manual_child_environment(root, gateway_key=state.get("gateway_key"))
    process = subprocess.Popen(
        _desktop_command(executable, root, launcher_strategy),
        cwd=executable.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    launch_index = len(state.get("app_launches", [])) + 1
    state["current_app_pid"] = process.pid
    state["current_leg"] = leg
    state.setdefault("app_launches", []).append(
        {
            "leg": leg,
            "pid": process.pid,
            "launch_index": launch_index,
            "started_at": _utc_now(),
            "desktop_label": f"desktop-{launch_index}",
            "build_version": version,
            "launcher_strategy": launcher_strategy,
        }
    )
    _write_state(root, state)
    _append_jsonl(
        _capture_path(root, "app-lifecycle.jsonl"),
        {
            "at": _utc_now(),
            "event": "desktop_launch_requested",
            "leg": leg,
            "desktop_label": f"desktop-{launch_index}",
            "build_version": version,
            "launcher_strategy": launcher_strategy,
        },
    )
    readiness = _await_desktop_launch_readiness(process)
    state["last_launcher_readiness"] = readiness
    _write_state(root, state)
    readiness_event = {
        "at": _utc_now(),
        "leg": leg,
        "desktop_label": f"desktop-{launch_index}",
        "launcher_strategy": launcher_strategy,
        "readiness_indicator": readiness.get("readiness_indicator"),
        "classification": readiness.get("classification"),
        "exit_code": readiness.get("exit_code"),
    }
    if readiness.get("status") != "ready":
        _append_jsonl(
            _capture_path(root, "app-lifecycle.jsonl"),
            {"event": "desktop_launch_not_ready", **readiness_event},
        )
        raise CaptureError(f"isolated_desktop_{readiness.get('classification', 'launch_not_ready')}")
    _append_jsonl(
        _capture_path(root, "app-lifecycle.jsonl"),
        {"event": "desktop_launch_ready", **readiness_event},
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    watcher = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_watch", "--session", str(state["session_id"])],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=creationflags,
    )
    state["watcher_pid"] = watcher.pid
    _write_state(root, state)
    return readiness


def prepare(model: str) -> dict[str, Any]:
    _install_root, version = _require_desktop_seams()
    parent = Path(tempfile.gettempdir()) / SESSION_PREFIX
    parent.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        session_id = secrets.token_hex(6)
        root = parent / session_id
        try:
            root.mkdir()
        except FileExistsError:
            continue
        break
    else:  # pragma: no cover - cryptographic collision guard.
        raise CaptureError("unable_to_allocate_isolated_session")
    paths = _runtime_paths(root)
    paths["codex_home"].mkdir(parents=True)
    paths["electron_user_data"].mkdir(parents=True)
    paths["config"].write_text(_base_config(model), encoding="utf-8")
    state: dict[str, Any] = {
        "schema_version": 1,
        "session_id": session_id,
        "created_at": _utc_now(),
        "build_version": version,
        "model": model,
        "session_hmac_key": secrets.token_hex(32),
        "proxy_mode": "auto_windows_registry",
        "gateway_overlay_active": False,
        "gateway_pid": None,
        "gateway_port": None,
        "gateway_key": None,
        "desktop_route_mode": "disposable_auth_bootstrap",
        "current_app_pid": None,
        "current_leg": None,
        "app_launches": [],
        "app_results": [],
    }
    _write_state(root, state)
    return {
        "status": "prepared",
        "session": session_id,
        "desktop_build_version": version,
        "model": model,
        "proxy_mode": "auto_windows_registry",
        "test_gateway": "disconnected_until_gateway_leg",
        "authentication": "manual_in_disposable_profile_only",
        "websockets": "disabled_for_gateway_leg",
    }


def _long_stream_configuration(state: dict[str, Any]) -> dict[str, Any] | None:
    configuration = state.get("long_stream")
    if configuration is None:
        return None
    if not isinstance(configuration, dict):
        raise CaptureError("invalid_long_stream_state")
    expected = {
        "protocol": LONG_STREAM_PROTOCOL,
        "prompt_identifier": LONG_STREAM_PROMPT_IDENTIFIER,
        "prompt_sha256": LONG_STREAM_PROMPT_SHA256,
        "target_duration_ms": LONG_STREAM_TARGET_DURATION_MS,
        "first_visible_output_semantics": LONG_STREAM_FIRST_VISIBLE_SEMANTICS,
    }
    if any(configuration.get(key) != value for key, value in expected.items()):
        raise CaptureError("invalid_long_stream_state")
    events = configuration.get("events")
    if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
        raise CaptureError("invalid_long_stream_state")
    return configuration


def arm_long_stream(session_id: str) -> dict[str, Any]:
    """Arm a fresh isolated session for the direct-Official long-stream control.

    The exact human prompt is deliberately not stored here.  Runtime evidence
    receives only its stable identifier and digest, never prompt or response
    content.
    """

    root = _session_root(session_id)
    state = _load_state(session_id)
    if state.get("long_stream") is not None:
        raise CaptureError("long_stream_already_armed")
    if (
        state.get("current_leg") is not None
        or state.get("app_launches")
        or state.get("app_results")
        or state.get("gateway_overlay_active")
        or state.get("gateway_pid") is not None
    ):
        raise CaptureError("long_stream_requires_fresh_unlaunched_session")
    state["long_stream"] = {
        "protocol": LONG_STREAM_PROTOCOL,
        "prompt_identifier": LONG_STREAM_PROMPT_IDENTIFIER,
        "prompt_sha256": LONG_STREAM_PROMPT_SHA256,
        "target_duration_ms": LONG_STREAM_TARGET_DURATION_MS,
        "first_visible_output_semantics": LONG_STREAM_FIRST_VISIBLE_SEMANTICS,
        "phase": "awaiting_first_visible_output",
        "events": [],
    }
    _write_state(root, state)
    return {
        "status": "long_stream_armed",
        "session": session_id,
        "protocol": LONG_STREAM_PROTOCOL,
        "prompt_identifier": LONG_STREAM_PROMPT_IDENTIFIER,
        "prompt_sha256": LONG_STREAM_PROMPT_SHA256,
        "target_duration_ms": LONG_STREAM_TARGET_DURATION_MS,
        "first_visible_output_semantics": LONG_STREAM_FIRST_VISIBLE_SEMANTICS,
        "route": "direct_official",
    }


def mark_long_stream(session_id: str, marker: str) -> dict[str, Any]:
    """Record one sanitized operator-observed marker for the long-stream run."""

    if marker not in LONG_STREAM_MARKERS:
        raise CaptureError("unsupported_long_stream_marker")
    root = _session_root(session_id)
    state = _load_state(session_id)
    configuration = _long_stream_configuration(state)
    if configuration is None:
        raise CaptureError("long_stream_not_armed")
    if state.get("current_leg") != "direct_official":
        raise CaptureError("long_stream_direct_official_leg_not_active")
    phase = configuration.get("phase")
    if marker == "first_visible_output":
        if phase != "awaiting_first_visible_output":
            raise CaptureError("long_stream_marker_out_of_order")
        next_phase = "awaiting_stream_active_target"
        lifecycle_event = "renderer_first_visible_output"
    else:
        if phase == "awaiting_first_visible_output":
            raise CaptureError("long_stream_first_visible_output_required")
        if phase != "awaiting_stream_active_target":
            raise CaptureError("long_stream_marker_out_of_order")
        next_phase = "awaiting_terminal"
        lifecycle_event = "renderer_stream_active_target_reached"

    at = _utc_now()
    events = configuration["events"]
    events.append({"sequence": len(events) + 1, "event": marker, "at": at})
    configuration["phase"] = next_phase
    _write_state(root, state)
    _append_jsonl(
        _capture_path(root, "app-cues.jsonl"),
        {
            "at": at,
            "event": lifecycle_event,
            "leg": "direct_official",
            "prompt_identifier": LONG_STREAM_PROMPT_IDENTIFIER,
        },
    )
    return {
        "status": "long_stream_marker_recorded",
        "session": session_id,
        "marker": marker,
        "target_duration_ms": LONG_STREAM_TARGET_DURATION_MS,
        "first_visible_output_semantics": LONG_STREAM_FIRST_VISIBLE_SEMANTICS,
    }


def _record_long_stream_terminal(configuration: dict[str, Any], result: str, at: str) -> None:
    events = configuration["events"]
    if any(item.get("event") == "terminal" for item in events):
        raise CaptureError("duplicate_long_stream_terminal")
    phase = configuration.get("phase")
    if phase not in {
        "awaiting_first_visible_output",
        "awaiting_stream_active_target",
        "awaiting_terminal",
    }:
        raise CaptureError("long_stream_terminal_out_of_order")
    events.append({"sequence": len(events) + 1, "event": "terminal", "at": at, "result": result})
    configuration["phase"] = "terminal_recorded"


def launch(session_id: str, leg: str, *, launcher_strategy: str = DEFAULT_LAUNCHER_STRATEGY) -> dict[str, Any]:
    if leg not in LEGS:
        raise CaptureError("unsupported_leg")
    if launcher_strategy not in LAUNCH_READINESS_STRATEGIES:
        raise CaptureError("unsupported_launcher_strategy")
    if leg == "direct_official":
        raise CaptureError("direct_official_control_cancelled")
    root = _session_root(session_id)
    state = _load_state(session_id)
    if state.get("long_stream") is not None and leg != "direct_official":
        raise CaptureError("long_stream_requires_direct_official")
    if _pid_alive(state.get("current_app_pid")):
        raise CaptureError("previous_isolated_desktop_instance_is_still_running")
    if leg == "gateway_official_auto":
        paths = _runtime_paths(root)
        if not (paths["codex_home"] / "auth.json").is_file():
            raise CaptureError("manual_login_required_in_disposable_profile")
        state["desktop_route_mode"] = "isolated_gateway_loopback"
        _write_state(root, state)
        _start_gateway(root, state)
        state = _load_state(session_id)
    try:
        readiness = _launch_desktop(root, state, leg, launcher_strategy)
    except CaptureError:
        if leg == "gateway_official_auto":
            failed_state = _load_state(session_id)
            failed_state["gateway_stop_after_launch_failure"] = _manual_stop_gateway(root, failed_state)
            failed_state["overlay_restore_after_launch_failure"] = _restore_isolated_overlay(root, failed_state)
            _write_state(root, failed_state)
        raise
    return {
        "status": "desktop_launched",
        "session": session_id,
        "leg": leg,
        "model": state.get("model"),
        "test_gateway": "connected" if leg == "gateway_official_auto" else "disconnected",
        "route": (
            "gateway_official_auto"
            if leg == "gateway_official_auto"
            else "disposable_auth_bootstrap"
            if leg == AUTH_BOOTSTRAP_LEG
            else "direct_official"
        ),
        "proxy_mode": "auto_windows_registry",
        "launcher_strategy": launcher_strategy,
        "startup_readiness": readiness,
    }


def mark(session_id: str, result: str) -> dict[str, Any]:
    if result not in APP_RESULTS:
        raise CaptureError("unsupported_app_result")
    root = _session_root(session_id)
    state = _load_state(session_id)
    leg = state.get("current_leg")
    if leg not in LEGS:
        raise CaptureError("desktop_leg_not_started")
    if leg == AUTH_BOOTSTRAP_LEG:
        raise CaptureError("auth_bootstrap_has_no_renderer_result")
    at = _utc_now()
    entry = {"at": at, "leg": leg, "result": result}
    configuration = _long_stream_configuration(state)
    if configuration is not None:
        if leg != "direct_official":
            raise CaptureError("long_stream_requires_direct_official")
        _record_long_stream_terminal(configuration, result, at)
        entry["long_stream_protocol"] = LONG_STREAM_PROTOCOL
    state.setdefault("app_results", []).append(entry)
    _write_state(root, state)
    _append_jsonl(_capture_path(root, "app-cues.jsonl"), {"event": "renderer_terminal_cue", **entry})
    return {"status": "marked", "session": session_id, "leg": leg, "result": result}


def _duration_between_timestamps_ms(start: object, end: object) -> int | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    duration_ms = round((end_at - start_at).total_seconds() * 1000)
    return duration_ms if duration_ms >= 0 else None


def _summarize_long_stream(state: dict[str, Any]) -> dict[str, Any] | None:
    configuration = state.get("long_stream")
    if configuration is None:
        return None
    summary: dict[str, Any] = {
        "protocol": LONG_STREAM_PROTOCOL,
        "prompt_identifier": LONG_STREAM_PROMPT_IDENTIFIER,
        "prompt_sha256": LONG_STREAM_PROMPT_SHA256,
        "target_duration_ms": LONG_STREAM_TARGET_DURATION_MS,
        "first_visible_output_semantics": LONG_STREAM_FIRST_VISIBLE_SEMANTICS,
        "first_visible_output_observed": False,
        "target_marker_recorded": False,
        "stream_active_target_reached": False,
        "terminal": None,
        "terminal_count": 0,
        "first_visible_to_target_ms": None,
        "first_visible_to_terminal_ms": None,
        "capture_status": "incomplete",
        "qualification": "invalid_marker_state",
    }
    if not isinstance(configuration, dict):
        return summary
    expected = {
        "protocol": LONG_STREAM_PROTOCOL,
        "prompt_identifier": LONG_STREAM_PROMPT_IDENTIFIER,
        "prompt_sha256": LONG_STREAM_PROMPT_SHA256,
        "target_duration_ms": LONG_STREAM_TARGET_DURATION_MS,
        "first_visible_output_semantics": LONG_STREAM_FIRST_VISIBLE_SEMANTICS,
    }
    if any(configuration.get(key) != value for key, value in expected.items()):
        return summary
    events = configuration.get("events")
    if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
        return summary

    allowed_events = {"first_visible_output", "stream_active_target_reached", "terminal"}
    malformed = False
    for sequence, item in enumerate(events, start=1):
        if item.get("sequence") != sequence or item.get("event") not in allowed_events or not isinstance(item.get("at"), str):
            malformed = True
        if item.get("event") == "terminal" and item.get("result") not in APP_RESULTS:
            malformed = True
    first_events = [item for item in events if item.get("event") == "first_visible_output"]
    target_events = [item for item in events if item.get("event") == "stream_active_target_reached"]
    terminal_events = [item for item in events if item.get("event") == "terminal"]
    summary["first_visible_output_observed"] = len(first_events) == 1
    summary["target_marker_recorded"] = len(target_events) == 1
    summary["terminal_count"] = len(terminal_events)
    if len(terminal_events) == 1:
        summary["terminal"] = terminal_events[0].get("result")

    first_index = first_events[0].get("sequence") if len(first_events) == 1 else None
    target_index = target_events[0].get("sequence") if len(target_events) == 1 else None
    terminal_index = terminal_events[0].get("sequence") if len(terminal_events) == 1 else None
    if not isinstance(first_index, int) and first_index is not None:
        malformed = True
    if not isinstance(target_index, int) and target_index is not None:
        malformed = True
    if not isinstance(terminal_index, int) and terminal_index is not None:
        malformed = True
    if target_index is not None and (first_index is None or target_index <= first_index):
        malformed = True
    if terminal_index is not None and terminal_index != len(events):
        malformed = True
    if terminal_index is not None and target_index is not None and terminal_index <= target_index:
        malformed = True
    if len(first_events) > 1 or len(target_events) > 1 or len(terminal_events) > 1:
        malformed = True

    first_at = first_events[0].get("at") if len(first_events) == 1 else None
    target_at = target_events[0].get("at") if len(target_events) == 1 else None
    terminal_at = terminal_events[0].get("at") if len(terminal_events) == 1 else None
    summary["first_visible_to_target_ms"] = _duration_between_timestamps_ms(first_at, target_at)
    summary["first_visible_to_terminal_ms"] = _duration_between_timestamps_ms(first_at, terminal_at)
    target_duration_met = (
        summary["first_visible_to_target_ms"] is not None
        and summary["first_visible_to_target_ms"] >= LONG_STREAM_TARGET_DURATION_MS
    )
    summary["stream_active_target_reached"] = bool(target_index is not None and target_duration_met and not malformed)

    expected_phase = "awaiting_first_visible_output"
    if terminal_index is not None:
        expected_phase = "terminal_recorded"
    elif target_index is not None:
        expected_phase = "awaiting_terminal"
    elif first_index is not None:
        expected_phase = "awaiting_stream_active_target"
    if configuration.get("phase") != expected_phase:
        malformed = True

    if malformed:
        return summary
    if target_index is not None and not target_duration_met:
        summary["qualification"] = "target_duration_not_met"
        return summary
    if first_index is not None and target_index is not None and terminal_index is not None:
        summary["capture_status"] = "complete"
        terminal = summary["terminal"]
        summary["qualification"] = (
            "sustained_control_observed"
            if terminal == "completed"
            else f"target_reached_{terminal}_observed"
        )
        return summary
    if terminal_index is not None:
        terminal = summary["terminal"]
        summary["qualification"] = (
            f"under_target_{terminal}"
            if first_index is not None
            else f"first_visible_output_missing_{terminal}"
        )
        return summary
    if first_index is not None and target_index is None:
        summary["qualification"] = "target_and_terminal_missing"
        return summary
    if first_index is not None and target_index is not None:
        summary["qualification"] = "terminal_missing"
        return summary
    summary["qualification"] = "first_visible_target_and_terminal_missing"
    return summary


def _manual_stop_gateway(root: Path, state: dict[str, Any]) -> str:
    port = state.get("gateway_port")
    gateway_key = state.get("gateway_key")
    pid = state.get("gateway_pid")
    if not isinstance(port, int) or not isinstance(gateway_key, str):
        return "not_started"
    try:
        request = Request(
            f"http://127.0.0.1:{port}/shutdown",
            data=b"",
            method="POST",
            headers={"Authorization": f"Bearer {gateway_key}"},
        )
        with urlopen(request, timeout=2):
            pass
    except (OSError, URLError):
        pass
    deadline = time.monotonic() + 5.0
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return "stopped" if not _pid_alive(pid) else "shutdown_requested"


def _restore_isolated_overlay(root: Path, state: dict[str, Any]) -> str:
    if not state.get("gateway_overlay_active"):
        return "not_applied"
    paths = _runtime_paths(root)
    try:
        result = config_overlay.restore_overlay(paths["config"], paths["overlay_backup"], unified_history=False)
    except (OSError, ValueError):
        return "restore_failed"
    state["gateway_overlay_active"] = False
    return str(result)


def _safe_event_value(value: Any) -> int | bool | str | None:
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, str):
        return value[:80]
    return None


def _current_gateway_transport_scope() -> dict[str, str]:
    """Describe the current Gateway transport boundary without a route or endpoint."""

    import codex_proxy

    private_official_pool = all(
        hasattr(codex_proxy, name)
        for name in ("OFFICIAL_HTTP_POOLS", "_official_pool_manager", "_OfficialHTTPSConnectionPool")
    )
    global_opener_installer = any(
        hasattr(codex_proxy, name)
        for name in ("_ensure_official_keepalive_opener_installed", "_OfficialKeepaliveHTTPSHandler")
    )
    external_urlopen = getattr(codex_proxy, "urlopen", None)
    return {
        "official_transport": "private_urllib3_pool" if private_official_pool else "unknown",
        "external_transport": "stdlib_urlopen"
        if getattr(external_urlopen, "__module__", None) == "urllib.request"
        else "unknown",
        "official_global_opener_installation": "present_current_source"
        if global_opener_installer
        else "absent_current_source",
    }


def _stdlib_global_opener_state() -> str:
    return "unset" if getattr(urllib_request, "_opener", None) is None else "set"


def _boundary_phase(value: object, *, default: str = "unknown") -> str:
    return value if isinstance(value, str) and value in BOUNDARY_FAILURE_PHASES else default


def _status_class(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value // 100 if 100 <= value <= 999 else None


def _safe_boundary_token(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else None


BOUNDARY_TRACE_INTEGER_FIELDS = {
    "status",
    "attempt",
    "max_attempts",
    "retry_budget",
    "status_class",
    "bytes",
    "line_index",
    "body_bytes",
    "body_expected",
    "bytes_exposed",
    "lines_exposed",
    "partial_bytes",
    "lines_streamed",
    "bytes_streamed",
    "last_upstream_byte_age_ms",
    "duration_ms",
    "delay_ms",
    "sse_events_streamed",
}
BOUNDARY_TRACE_BOOLEAN_FIELDS = {
    "line_complete",
    "sse_event_complete",
    "terminal_observed",
    "completed_terminal_observed",
    "terminal_forwarded",
    "close_requested",
    "official_global_opener_changed",
    "client_disconnected",
    "synthetic_terminal_event_sent",
    "headers_sent_downstream",
    "downstream_sse_started",
    "sse_terminal_event_seen",
    "sse_completed_event_seen",
    "sse_downstream_output_seen",
}
BOUNDARY_TRACE_TOKEN_FIELDS = {
    "protocol",
    "configured_proxy_mode",
    "effective_proxy_mode",
    "desktop_route_mode",
    "app_stream_consumer_boundary",
    "connection_disposition",
    "error",
    "gateway_event",
    "terminal_kind",
    "official_global_opener_before",
    "official_global_opener_after",
    "official_transport",
    "external_transport",
    "official_global_opener_installation",
    "reported_failure_phase",
    "reported_failure_side",
    "reported_failure_class",
    "synthetic_terminal_event_type",
}


def _normalize_boundary_trace(trace: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool, str]:
    """Keep only ordered, content-free boundary events.

    Old capture rows intentionally have no sequence number and are ignored.  A
    malformed new row invalidates classification rather than allowing a later
    exception name to stand in for an ordering decision.
    """

    rows: list[dict[str, Any]] = []
    expected_sequence = 1
    previous_elapsed = -1
    for item in trace:
        if "sequence" not in item:
            continue
        sequence = item.get("sequence")
        elapsed_ms = item.get("elapsed_ms")
        event = _safe_boundary_token(item.get("event"))
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != expected_sequence
            or isinstance(elapsed_ms, bool)
            or not isinstance(elapsed_ms, int)
            or elapsed_ms < previous_elapsed
            or event is None
        ):
            return [], False, "trace_order_invalid"
        expected_sequence += 1
        previous_elapsed = elapsed_ms
        row: dict[str, Any] = {"sequence": sequence, "elapsed_ms": elapsed_ms, "event": event}
        raw_request = item.get("request")
        raw_downstream = item.get("downstream_connection")
        raw_upstream = item.get("upstream_connection")
        if isinstance(raw_request, str) and raw_request:
            row["_raw_request"] = raw_request
        if isinstance(raw_downstream, str) and raw_downstream:
            row["_raw_downstream_connection"] = raw_downstream
        if isinstance(raw_upstream, str) and raw_upstream:
            row["_raw_upstream_connection"] = raw_upstream
        failure_phase = _boundary_phase(item.get("failure_phase"), default="")
        if failure_phase:
            row["failure_phase"] = failure_phase
        for field in BOUNDARY_TRACE_INTEGER_FIELDS:
            value = item.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                row[field] = value
        for field in BOUNDARY_TRACE_BOOLEAN_FIELDS:
            value = item.get(field)
            if isinstance(value, bool):
                row[field] = value
        for field in BOUNDARY_TRACE_TOKEN_FIELDS:
            value = _safe_boundary_token(item.get(field))
            if value is not None:
                row[field] = value
        rows.append(row)
    if not rows:
        return [], False, "boundary_trace_missing"
    return rows, True, "no_boundary_failure_observed"


def _boundary_candidate(row: dict[str, Any]) -> tuple[str, str] | None:
    event = row.get("event")
    if event in {"downstream_body_read_failed", "downstream_body_short_read"}:
        return "downstream_client", "downstream_body"
    if event == "downstream_write_failed":
        return "downstream_client", "downstream_write"
    if event in {"gateway_relay_terminal_not_forwarded", "gateway_relay_exception_after_terminal"}:
        return "gateway_relay", "relay_terminalization"
    return None


UPSTREAM_TRANSPORT_BOUNDARY_EVENTS = frozenset(
    {
        "upstream_dns_failed",
        "upstream_tcp_connect_failed",
        "upstream_tls_handshake_failed",
        "upstream_request_write_failed",
        "upstream_open_failed",
        "upstream_sse_read_failed",
        "upstream_sse_eof_without_terminal",
    }
)
DOWNSTREAM_WRITABLE_AFTER_UPSTREAM_FAILURE_EVENTS = frozenset(
    {
        "downstream_write_after_upstream_failure_succeeded",
    }
)


def _first_supported_boundary_candidate(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], str, str] | None:
    """Classify only a boundary whose observed ordering proves its direction.

    An exception raised by the upstream reader is a useful phase marker but is
    not by itself proof that the remote peer closed first.  It becomes an
    upstream-transport result only when a later successful Gateway-to-Desktop
    write proves that the downstream connection remained writable.  Otherwise
    the trace fails closed as ``unknown``.
    """

    ordered = list(events)
    unresolved_upstream_requests: set[str | None] = set()
    for index, row in enumerate(ordered):
        event = row.get("event")
        if event in UPSTREAM_TRANSPORT_BOUNDARY_EVENTS:
            request = row.get("request")
            downstream_writable = any(
                later.get("request") == request
                and later.get("event") in DOWNSTREAM_WRITABLE_AFTER_UPSTREAM_FAILURE_EVENTS
                for later in ordered[index + 1 :]
            )
            if downstream_writable:
                return row, "upstream_transport", _boundary_phase(row.get("failure_phase"))
            unresolved_upstream_requests.add(request if isinstance(request, str) else None)
            continue
        candidate = _boundary_candidate(row)
        if candidate is None:
            continue
        request = row.get("request")
        if (request if isinstance(request, str) else None) in unresolved_upstream_requests:
            return None
        return row, *candidate
    return None


def _new_boundary_request_summary() -> dict[str, Any]:
    return {
        "route": {
            "protocol": None,
            "configured_proxy_mode": None,
            "effective_proxy_mode": None,
            "desktop_route_mode": None,
        },
        "downstream": {
            "request_observed": False,
            "stream_consumer_boundary": None,
            "response_opened": False,
            "response_headers_sent": False,
            "first_exposed": False,
            "write_failure_observed": False,
        },
        "connections": {"identities": [], "dispositions": []},
        "retry": {"attempts_observed": [], "max_attempts": None, "configured_budget": None},
        "upstream": {
            "lines_received": 0,
            "bytes_received": 0,
            "first_sse_elapsed_ms": None,
            "last_complete_sse_elapsed_ms": None,
            "partial_line_observed": False,
            "eof_observed": False,
            "protocol_terminal_observed": False,
            "completed_terminal_observed": False,
        },
        "relay": {
            "bytes_exposed": 0,
            "lines_exposed": 0,
            "terminal_forwarded": False,
            "local_socket_closed": False,
        },
        "_gateway_terminals": [],
    }


def _summarize_gateway_boundary_trace(trace: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows, trace_order_valid, classification_reason = _normalize_boundary_trace(trace)
    request_labels: dict[str, str] = {}
    downstream_labels: dict[str, str] = {}
    upstream_labels: dict[str, str] = {}

    def label(raw: object, prefix: str, labels: dict[str, str]) -> str | None:
        if not isinstance(raw, str) or not raw:
            return None
        return labels.setdefault(raw, f"{prefix}-{len(labels) + 1}")

    events: list[dict[str, Any]] = []
    request_summaries: dict[str, dict[str, Any]] = {}
    transport_scope: dict[str, str] = {}
    for source_row in rows:
        row = {key: value for key, value in source_row.items() if not key.startswith("_raw_")}
        request = label(source_row.get("_raw_request"), "request", request_labels)
        downstream = label(source_row.get("_raw_downstream_connection"), "downstream", downstream_labels)
        upstream = label(source_row.get("_raw_upstream_connection"), "upstream", upstream_labels)
        if request is not None:
            row["request"] = request
        if downstream is not None:
            row["downstream_connection"] = downstream
        if upstream is not None:
            row["upstream_connection"] = upstream
        events.append(row)

        if row["event"] == "gateway_trace_started":
            for key in ("official_transport", "external_transport", "official_global_opener_installation"):
                value = row.get(key)
                if isinstance(value, str):
                    transport_scope[key] = value
        if request is None:
            continue
        summary = request_summaries.setdefault(request, _new_boundary_request_summary())
        route = summary["route"]
        for key in ("protocol", "configured_proxy_mode", "effective_proxy_mode", "desktop_route_mode"):
            value = row.get(key)
            if isinstance(value, str):
                route[key] = value
        downstream_summary = summary["downstream"]
        if row["event"] == "downstream_request_bound":
            downstream_summary["request_observed"] = True
            boundary = row.get("app_stream_consumer_boundary")
            if isinstance(boundary, str):
                downstream_summary["stream_consumer_boundary"] = boundary
        if row["event"] == "downstream_response_open":
            downstream_summary["response_opened"] = True
        if row["event"] == "downstream_response_headers_sent":
            downstream_summary["response_headers_sent"] = True
        if row["event"] == "downstream_first_exposed":
            downstream_summary["first_exposed"] = True
        if row["event"] == "downstream_write_failed":
            downstream_summary["write_failure_observed"] = True
        retry = summary["retry"]
        attempt = row.get("attempt")
        if isinstance(attempt, int) and attempt not in retry["attempts_observed"]:
            retry["attempts_observed"].append(attempt)
        max_attempts = row.get("max_attempts")
        if isinstance(max_attempts, int):
            retry["max_attempts"] = max(max_attempts, retry["max_attempts"] or 0)
        retry_budget = row.get("retry_budget")
        if isinstance(retry_budget, int):
            retry["configured_budget"] = retry_budget
        connections = summary["connections"]
        if upstream is not None and upstream not in connections["identities"]:
            connections["identities"].append(upstream)
        disposition = row.get("connection_disposition")
        if isinstance(disposition, str):
            connections["dispositions"].append(disposition)
        upstream_summary = summary["upstream"]
        if row["event"] == "upstream_sse_line":
            upstream_summary["lines_received"] = max(
                upstream_summary["lines_received"], int(row.get("line_index", 0))
            )
            upstream_summary["bytes_received"] += int(row.get("bytes", 0))
            if upstream_summary["first_sse_elapsed_ms"] is None:
                upstream_summary["first_sse_elapsed_ms"] = row["elapsed_ms"]
            # A physical newline is not necessarily a complete SSE event.  Keep
            # the last-event marker tied to the live parser transition so a
            # partial event cannot be mistaken for a clean terminal boundary.
            if row.get("sse_event_complete") is True:
                upstream_summary["last_complete_sse_elapsed_ms"] = row["elapsed_ms"]
        if row["event"] == "upstream_sse_partial_line":
            upstream_summary["partial_line_observed"] = True
        if row["event"] == "upstream_sse_eof":
            upstream_summary["eof_observed"] = True
        if row["event"] == "upstream_protocol_terminal_observed":
            upstream_summary["protocol_terminal_observed"] = True
            upstream_summary["completed_terminal_observed"] = row.get("completed_terminal_observed") is True
        relay = summary["relay"]
        if row["event"] == "relay_exposure_summary":
            relay["bytes_exposed"] = max(relay["bytes_exposed"], int(row.get("bytes_exposed", 0)))
            relay["lines_exposed"] = max(relay["lines_exposed"], int(row.get("lines_exposed", 0)))
        if row["event"] == "relay_terminal_forwarded":
            relay["terminal_forwarded"] = True
        if row["event"] == "gateway_local_socket_closed":
            relay["local_socket_closed"] = True
        if row["event"] == "gateway_event" and row.get("gateway_event") in {"request_complete", "request_error"}:
            summary["_gateway_terminals"].append(str(row["gateway_event"]))
    rendered_requests: dict[str, dict[str, Any]] = {}
    for request, summary in request_summaries.items():
        terminals = summary.pop("_gateway_terminals")
        terminal_count = len(terminals)
        summary["terminal"] = {
            "gateway_terminal_outcome": terminals[0] if terminal_count == 1 else "duplicate" if terminal_count > 1 else "not_observed",
            "gateway_terminal_count": terminal_count,
            "silent_gateway_terminal": terminal_count == 0,
            "protocol_terminal_observed": summary["upstream"]["protocol_terminal_observed"],
            "completed_terminal_observed": summary["upstream"]["completed_terminal_observed"],
            "terminal_forwarded": summary["relay"]["terminal_forwarded"],
        }
        rendered_requests[request] = summary

    first_closing_side = "unknown"
    first_failure_phase = "unknown"
    classification_event: dict[str, Any] | None = None
    first_candidate = _first_supported_boundary_candidate(events) if trace_order_valid else None
    if first_candidate is not None:
        candidate_row, first_closing_side, first_failure_phase = first_candidate
        classification_event = {
            key: candidate_row[key]
            for key in ("sequence", "elapsed_ms", "event", "request")
            if key in candidate_row
        }
        classification_reason = "monotonic_boundary_trace_with_downstream_writability"
    elif not trace_order_valid:
        classification_reason = "trace_order_invalid" if rows else classification_reason

    downstream_boundaries = sorted(
        {
            str(summary["downstream"]["stream_consumer_boundary"])
            for summary in rendered_requests.values()
            if isinstance(summary["downstream"].get("stream_consumer_boundary"), str)
        }
    )
    desktop_downstream = {
        "request_count": sum(1 for summary in rendered_requests.values() if summary["downstream"]["request_observed"]),
        "stream_consumer_boundaries": downstream_boundaries,
    }

    return {
        "schema_version": BOUNDARY_TRACE_SCHEMA_VERSION,
        "trace_order_valid": trace_order_valid,
        "classification_reason": classification_reason,
        "first_closing_side": first_closing_side if first_closing_side in BOUNDARY_CLOSING_SIDES else "unknown",
        "first_failure_phase": _boundary_phase(first_failure_phase),
        "classification_event": classification_event,
        "transport_scope": transport_scope,
        "desktop_downstream": desktop_downstream,
        "request_count": len(rendered_requests),
        "requests": rendered_requests,
        "events": events,
    }


def _summarize_gateway_events(state: dict[str, Any], events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    labels: dict[str, str] = {}
    event_rows: list[dict[str, Any]] = []
    terminal_by_request: dict[str, str] = {}
    for item in events:
        event = item.get("event")
        if not isinstance(event, str):
            continue
        raw_request = item.get("request_id")
        request_label: str | None = None
        if isinstance(raw_request, str) and raw_request:
            request_label = labels.setdefault(raw_request, f"request-{len(labels) + 1}")
        row: dict[str, Any] = {"event": event}
        if request_label is not None:
            row["request"] = request_label
        for field in GATEWAY_EVENT_FIELDS:
            value = _safe_event_value(item.get(field))
            if value is not None:
                row[field] = value
        if event in {"request_complete", "request_error"} and request_label is not None:
            terminal_by_request[request_label] = event
        if event in {
            "request_start",
            "upstream_retry",
            "official_passthrough_stream_closed",
            "client_write_failed",
            "request_complete",
            "request_error",
        }:
            event_rows.append(row)
    return {
        "request_count": len(labels),
        "events": event_rows,
        "terminal_by_request": terminal_by_request,
        "first_closing_side": "unknown",
        "classification_source": "boundary_trace_required",
        "silent_terminal_request_count": sum(
            1 for label in labels.values() if label not in terminal_by_request
        ),
    }


def _summarize_app_lifecycle(root: Path) -> dict[str, Any]:
    events = _read_jsonl(_capture_path(root, "app-lifecycle.jsonl"))
    app_server_observed = any(item.get("role") == "app_server" for item in events)
    return {
        "desktop_launches": sum(1 for item in events if item.get("event") == "desktop_launch_requested"),
        "app_server_observed": app_server_observed,
        "events": [
            {
                key: item[key]
                for key in ("event", "leg", "desktop_label", "role", "process_label", "build_version")
                if key in item
            }
            for item in events
        ],
    }


def _capture_readiness_after_collection(state: dict[str, Any], boundary_trace: dict[str, Any]) -> dict[str, Any]:
    """State the next safe action without silently scheduling a control rerun."""

    results = state.get("app_results")
    latest = results[-1] if isinstance(results, list) and results and isinstance(results[-1], dict) else {}
    if latest.get("leg") != "gateway_official_auto":
        return {"status": "not_applicable"}
    if boundary_trace.get("first_closing_side") == "unknown":
        return {
            "status": "retained_for_next_natural_faulty_window",
            "automatic_retest": False,
            "rearm": "new_disposable_gateway_only_session",
            "direct_official_control": "cancelled",
        }
    return {
        "status": "first_close_localization_collected",
        "automatic_retest": False,
        "next_action": "choose_one_causal_probe_only_after_review",
        "direct_official_control": "cancelled",
    }


def collect(session_id: str, *, aborted: bool = False) -> dict[str, Any]:
    root = _session_root(session_id)
    state = _load_state(session_id)
    long_stream = _summarize_long_stream(state)
    desktop_process = {
        "state": "not_running",
        "identity_verified": False,
        "visible_main_window": False,
        "responsive": False,
    }
    if _pid_alive(state.get("current_app_pid")):
        desktop_process = _classify_isolated_background_desktop(root, state)
        if not (
            desktop_process.get("state") == "background_after_normal_close"
            and isinstance(state.get("app_results"), list)
            and state["app_results"]
        ):
            raise CaptureError("close_isolated_desktop_before_collection")
        _append_jsonl(
            _capture_path(root, "app-lifecycle.jsonl"),
            {
                "at": _utc_now(),
                "event": "desktop_background_after_normal_close",
                "leg": state.get("current_leg"),
            },
        )
    paths = _runtime_paths(root)
    paths["watch_stop"].parent.mkdir(parents=True, exist_ok=True)
    paths["watch_stop"].touch()
    gateway_stop = _manual_stop_gateway(root, state)
    overlay_restore = _restore_isolated_overlay(root, state)
    state["gateway_stop"] = gateway_stop
    state["overlay_restore"] = overlay_restore
    state["desktop_process_close_state"] = desktop_process["state"]
    state["collected_at"] = _utc_now()
    _write_state(root, state)
    status = (
        "aborted_with_background_process"
        if aborted and desktop_process["state"] == "background_after_normal_close"
        else "aborted"
        if aborted
        else "collected_with_background_process"
        if desktop_process["state"] == "background_after_normal_close"
        else "collected"
    )
    if long_stream is not None and long_stream["capture_status"] != "complete":
        status = (
            "aborted_incomplete_long_stream_capture"
            if aborted
            else "incomplete_long_stream_capture_with_background_process"
            if desktop_process["state"] == "background_after_normal_close"
            else "incomplete_long_stream_capture"
        )
    boundary_trace = _summarize_gateway_boundary_trace(_read_jsonl(_capture_path(root, "gateway-trace.jsonl")))
    desktop_downstream = boundary_trace["desktop_downstream"]
    desktop_boundary = {
        "desktop_build_identity": "package_build",
        "desktop_build_version": state.get("build_version"),
        "app_server_rollout_identity": "not_exposed_by_supported_isolation_seam",
        "configured_route_mode": state.get("desktop_route_mode"),
        "app_stream_consumer_boundaries": desktop_downstream["stream_consumer_boundaries"],
        "gateway_downstream_request_observed": desktop_downstream["request_count"] > 0,
    }
    report = {
        "schema_version": 4,
        "status": status,
        "session": session_id,
        "desktop_build_version": state.get("build_version"),
        "model": state.get("model"),
        "proxy_mode": state.get("proxy_mode"),
        "legs": [item.get("leg") for item in state.get("app_launches", []) if isinstance(item, dict)],
        "app_renderer_cues": [
            {key: item[key] for key in ("leg", "result") if key in item}
            for item in state.get("app_results", [])
            if isinstance(item, dict)
        ],
        "cli_negative_control": state.get("cli_negative_control"),
        "app_lifecycle": _summarize_app_lifecycle(root),
        "desktop_boundary": desktop_boundary,
        "gateway": _summarize_gateway_events(state, _read_jsonl(paths["gateway_events"])),
        "gateway_connection_trace": boundary_trace,
        "gateway_boundary": boundary_trace,
        "long_stream": long_stream,
        "desktop_process": desktop_process,
        "session_reusable": False,
        "capture_readiness": _capture_readiness_after_collection(state, boundary_trace),
        "cleanup": {
            "gateway": gateway_stop,
            "isolated_config_overlay": overlay_restore,
            "desktop_process_teardown": (
                "not_permitted"
                if desktop_process["state"] == "background_after_normal_close"
                else "not_needed"
            ),
            "shared_state_touched": False,
        },
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    _capture_path(root, "report.json").write_text(rendered + "\n", encoding="utf-8")
    return report


def _process_children(parent_pid: int) -> list[tuple[int, str]]:
    if os.name != "nt" or parent_pid <= 0:
        return []
    command = (
        f"Get-CimInstance Win32_Process -Filter 'ParentProcessId = {parent_pid}' | "
        "Select-Object ProcessId,Name | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    result: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("ProcessId")
        name = row.get("Name")
        if isinstance(pid, int) and isinstance(name, str):
            result.append((pid, name))
    return result


def _watch(session_id: str) -> int:
    root = _session_root(session_id)
    state = _load_state(session_id)
    app_pid = state.get("current_app_pid")
    if not isinstance(app_pid, int):
        return 2
    trace_path = _capture_path(root, "app-lifecycle.jsonl")
    known: dict[int, str] = {app_pid: "desktop"}
    emitted: set[int] = set()
    empty_polls = 0
    while True:
        if _runtime_paths(root)["watch_stop"].exists():
            _append_jsonl(trace_path, {"at": _utc_now(), "event": "watch_stopped"})
            return 0
        for parent_pid in list(known):
            for child_pid, name in _process_children(parent_pid):
                if child_pid not in known:
                    known[child_pid] = "app_server" if "codex" in name.casefold() else "desktop_child"
        live = [pid for pid in known if _pid_alive(pid)]
        for pid, role in known.items():
            if pid in emitted:
                continue
            emitted.add(pid)
            _append_jsonl(
                trace_path,
                {
                    "at": _utc_now(),
                    "event": "process_observed",
                    "role": role,
                    "process_label": _opaque_label(state, "process", pid),
                    "leg": state.get("current_leg"),
                },
            )
        if live:
            empty_polls = 0
        else:
            empty_polls += 1
            if empty_polls >= 3:
                _append_jsonl(trace_path, {"at": _utc_now(), "event": "desktop_process_tree_ended"})
                return 0
        time.sleep(0.5)


def _response_connection_identity(response: Any) -> object | None:
    """Return a real socket handle, never a per-response wrapper fallback."""

    raw_response = getattr(response, "_response", response)
    candidates = (response, raw_response)
    for candidate in candidates:
        connection = getattr(candidate, "connection", None)
        socket_value = getattr(connection, "sock", None)
        if socket_value is not None:
            return socket_value
        direct_socket = getattr(candidate, "sock", None)
        if direct_socket is not None:
            return direct_socket
    for candidate in candidates:
        file_pointer = getattr(candidate, "fp", None)
        raw_stream = getattr(file_pointer, "raw", None)
        socket_value = getattr(raw_stream, "_sock", None)
        if socket_value is not None:
            return socket_value
        connection = getattr(raw_stream, "_connection", None)
        socket_value = getattr(connection, "sock", None)
        if socket_value is not None:
            return socket_value
    return None


def _live_upstream_phase(request_state: dict[str, Any] | None) -> str:
    if request_state is None:
        return "unknown"
    return _boundary_phase(request_state.get("last_upstream_phase"))


def _complete_sse_line(data: bytes) -> bool:
    return data.endswith((b"\n", b"\r"))


class _BoundaryTraceReader:
    def __init__(
        self,
        reader: Any,
        *,
        request_state: dict[str, Any],
        expected_bytes: int,
        record: Callable[[dict[str, Any]], None],
    ) -> None:
        self._reader = reader
        self._request_state = request_state
        self._expected_bytes = expected_bytes
        self._record = record
        self._started = False

    def read(self, *args: Any, **kwargs: Any) -> Any:
        if not self._started:
            self._started = True
            self._record(
                {
                    "event": "downstream_body_read_begin",
                    "request": self._request_state.get("request"),
                    "downstream_connection": self._request_state.get("downstream_connection"),
                    "body_expected": self._expected_bytes,
                }
            )
        try:
            value = self._reader.read(*args, **kwargs)
        except OSError as exc:
            self._record(
                {
                    "event": "downstream_body_read_failed",
                    "request": self._request_state.get("request"),
                    "downstream_connection": self._request_state.get("downstream_connection"),
                    "failure_phase": "downstream_body",
                    "error": type(exc).__name__,
                }
            )
            raise
        body_bytes = len(value) if isinstance(value, (bytes, bytearray, memoryview)) else 0
        event = "downstream_body_read_complete" if body_bytes >= self._expected_bytes else "downstream_body_short_read"
        payload: dict[str, Any] = {
            "event": event,
            "request": self._request_state.get("request"),
            "downstream_connection": self._request_state.get("downstream_connection"),
            "body_expected": self._expected_bytes,
            "body_bytes": body_bytes,
        }
        if event == "downstream_body_short_read":
            payload["failure_phase"] = "downstream_body"
        self._record(payload)
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._reader, name)


class _BoundaryTraceWriter:
    def __init__(
        self,
        writer: Any,
        *,
        request_state: dict[str, Any],
        record: Callable[[dict[str, Any]], None],
    ) -> None:
        self._writer = writer
        self._request_state = request_state
        self._record = record

    def _record_write_failure(self, operation: str, exc: OSError) -> None:
        self._request_state["downstream_write_failed"] = True
        self._record(
            {
                "event": "downstream_write_failed",
                "request": self._request_state.get("request"),
                "downstream_connection": self._request_state.get("downstream_connection"),
                "failure_phase": "downstream_write",
                "error": type(exc).__name__,
                "terminal_observed": bool(self._request_state["upstream_sse_stats"].terminal_event_seen),
                "close_requested": operation == "flush",
            }
        )

    def write(self, data: Any) -> Any:
        payload = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else b""
        try:
            result = self._writer.write(data)
        except OSError as exc:
            self._record_write_failure("write", exc)
            raise
        request_state = self._request_state
        if (
            payload
            and request_state.get("upstream_failure_observed")
            and not request_state.get("downstream_post_upstream_failure_write_confirmed")
        ):
            request_state["downstream_post_upstream_failure_write_confirmed"] = True
            self._record(
                {
                    "event": "downstream_write_after_upstream_failure_succeeded",
                    "request": request_state.get("request"),
                    "downstream_connection": request_state.get("downstream_connection"),
                    "bytes": len(payload),
                }
            )
        if not payload or not self._request_state["downstream_headers_complete"]:
            return result
        request_state["downstream_bytes_exposed"] += len(payload)
        request_state["downstream_lines_exposed"] += payload.count(b"\n")
        if not request_state["downstream_first_exposed"]:
            request_state["downstream_first_exposed"] = True
            self._record(
                {
                    "event": "downstream_first_exposed",
                    "request": request_state.get("request"),
                    "downstream_connection": request_state.get("downstream_connection"),
                    "bytes": len(payload),
                }
            )
        stats = request_state["downstream_sse_stats"]
        terminal_before = bool(stats.terminal_event_seen)
        stats.observe_line(payload)
        if stats.terminal_event_seen and not terminal_before and not request_state["downstream_terminal_forwarded"]:
            request_state["downstream_terminal_forwarded"] = True
            self._record(
                {
                    "event": "relay_terminal_forwarded",
                    "request": request_state.get("request"),
                    "downstream_connection": request_state.get("downstream_connection"),
                    "terminal_forwarded": True,
                    "terminal_kind": "response_completed" if stats.completed_event_seen else "protocol_terminal",
                    "bytes_exposed": request_state["downstream_bytes_exposed"],
                    "lines_exposed": request_state["downstream_lines_exposed"],
                }
            )
        return result

    def flush(self) -> Any:
        try:
            return self._writer.flush()
        except OSError as exc:
            self._record_write_failure("flush", exc)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._writer, name)


def _wrap_readline(
    response: Any,
    *,
    record: Callable[[dict[str, Any]], None],
    request_state: dict[str, Any],
    protocol: str,
) -> Any:
    original = getattr(response, "readline", None)
    if not callable(original):
        return response

    def traced_readline(*args: Any, **kwargs: Any) -> Any:
        try:
            line = original(*args, **kwargs)
        except BaseException as exc:
            request_state["upstream_failure_observed"] = True
            partial = getattr(exc, "partial", b"")
            try:
                partial_bytes = len(partial)
            except TypeError:
                partial_bytes = 0
            record(
                {
                    "event": "upstream_sse_read_failed",
                    "request": request_state.get("request"),
                    "protocol": protocol,
                    "failure_phase": _live_upstream_phase(request_state),
                    "error": type(exc).__name__,
                    "partial_bytes": partial_bytes,
                }
            )
            raise
        data = bytes(line) if isinstance(line, (bytes, bytearray, memoryview)) else b""
        stats = request_state["upstream_sse_stats"]
        if not data:
            terminal_observed = bool(stats.terminal_event_seen)
            record(
                {
                    "event": "upstream_sse_eof",
                    "request": request_state.get("request"),
                    "protocol": protocol,
                    "terminal_observed": terminal_observed,
                    "completed_terminal_observed": bool(stats.completed_event_seen),
                }
            )
            if not terminal_observed:
                request_state["upstream_failure_observed"] = True
                record(
                    {
                        "event": "upstream_sse_eof_without_terminal",
                        "request": request_state.get("request"),
                        "protocol": protocol,
                        "failure_phase": _live_upstream_phase(request_state),
                    }
                )
            return line
        request_state["last_upstream_phase"] = "sse_read"
        request_state["upstream_lines"] += 1
        request_state["upstream_bytes"] += len(data)
        if not request_state["upstream_first_sse"]:
            request_state["upstream_first_sse"] = True
            record(
                {
                    "event": "upstream_sse_first",
                    "request": request_state.get("request"),
                    "protocol": protocol,
                    "bytes": len(data),
                }
            )
        terminal_before = bool(stats.terminal_event_seen)
        events_before = stats.events_streamed
        stats.observe_line(data)
        line_complete = _complete_sse_line(data)
        record(
            {
                "event": "upstream_sse_line",
                "request": request_state.get("request"),
                "protocol": protocol,
                "line_index": request_state["upstream_lines"],
                "bytes": len(data),
                "line_complete": line_complete,
                "sse_event_complete": stats.events_streamed > events_before,
                "terminal_observed": bool(stats.terminal_event_seen),
                "completed_terminal_observed": bool(stats.completed_event_seen),
            }
        )
        if not line_complete:
            record(
                {
                    "event": "upstream_sse_partial_line",
                    "request": request_state.get("request"),
                    "protocol": protocol,
                    "bytes": len(data),
                }
            )
        if stats.terminal_event_seen and not terminal_before:
            request_state["upstream_terminal_observed"] = True
            record(
                {
                    "event": "upstream_protocol_terminal_observed",
                    "request": request_state.get("request"),
                    "protocol": protocol,
                    "terminal_observed": True,
                    "completed_terminal_observed": bool(stats.completed_event_seen),
                    "terminal_kind": "response_completed" if stats.completed_event_seen else "protocol_terminal",
                }
            )
        return line

    try:
        setattr(response, "readline", traced_readline)
    except (AttributeError, TypeError):
        pass
    return response


def _install_gateway_trace(root: Path, session_state: dict[str, Any]) -> None:
    """Install task-owned, content-free first-close instrumentation in the Gateway child."""

    import codex_proxy

    trace_path = _capture_path(root, "gateway-trace.jsonl")
    trace_lock = threading.Lock()
    connection_lock = threading.Lock()
    context = threading.local()
    trace_started_at = time.monotonic()
    sequence = 0
    observed_connections: set[str] = set()

    def record(payload: dict[str, Any]) -> None:
        nonlocal sequence
        event = _safe_boundary_token(payload.get("event"))
        if event is None:
            return
        compact: dict[str, Any] = {"schema_version": BOUNDARY_TRACE_SCHEMA_VERSION, "event": event}
        for field in ("request", "downstream_connection", "upstream_connection"):
            value = _safe_boundary_token(payload.get(field))
            if value is not None:
                compact[field] = value
        failure_phase = _boundary_phase(payload.get("failure_phase"), default="")
        if failure_phase:
            compact["failure_phase"] = failure_phase
        for field in BOUNDARY_TRACE_INTEGER_FIELDS:
            value = payload.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                compact[field] = value
        for field in BOUNDARY_TRACE_BOOLEAN_FIELDS:
            value = payload.get(field)
            if isinstance(value, bool):
                compact[field] = value
        for field in BOUNDARY_TRACE_TOKEN_FIELDS:
            value = _safe_boundary_token(payload.get(field))
            if value is not None:
                compact[field] = value
        with trace_lock:
            sequence += 1
            compact["sequence"] = sequence
            compact["elapsed_ms"] = int(max(0.0, time.monotonic() - trace_started_at) * 1000)
            _append_jsonl(trace_path, compact)

    def current_request_state() -> dict[str, Any] | None:
        value = getattr(context, "request_state", None)
        return value if isinstance(value, dict) else None

    def emit(event: str, request_state: dict[str, Any] | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {"event": event, **fields}
        if request_state is not None:
            payload.setdefault("request", request_state.get("request"))
            payload.setdefault("downstream_connection", request_state.get("downstream_connection"))
        record(payload)

    def bind_request(request_state: dict[str, Any], raw_request: object) -> None:
        if not isinstance(raw_request, str) or not raw_request:
            return
        request_label = _opaque_label(session_state, "gateway_request", raw_request)
        if request_state.get("request") == request_label:
            return
        request_state["request"] = request_label
        emit(
            "downstream_request_bound",
            request_state,
            desktop_route_mode=request_state["desktop_route_mode"],
            app_stream_consumer_boundary=request_state["app_stream_consumer_boundary"],
        )

    def new_request_state(peer: object) -> dict[str, Any]:
        return {
            "request": None,
            "downstream_connection": _opaque_label(session_state, "downstream_connection", peer),
            "protocol": None,
            "desktop_route_mode": str(session_state.get("desktop_route_mode") or "unknown"),
            "app_stream_consumer_boundary": "gateway_downstream_socket",
            "attempt": 1,
            "retry_budget": None,
            "last_upstream_phase": "unknown",
            "upstream_sse_stats": codex_proxy.PassthroughSseSemanticStats(),
            "downstream_sse_stats": codex_proxy.PassthroughSseSemanticStats(),
            "upstream_first_sse": False,
            "upstream_lines": 0,
            "upstream_bytes": 0,
            "upstream_terminal_observed": False,
            "downstream_first_exposed": False,
            "downstream_bytes_exposed": 0,
            "downstream_lines_exposed": 0,
            "downstream_terminal_forwarded": False,
            "downstream_write_failed": False,
            "downstream_headers_complete": False,
            "upstream_failure_observed": False,
            "downstream_post_upstream_failure_write_confirmed": False,
            "trace_finalized": False,
        }

    def effective_proxy_mode(protocol: str, request: Any) -> str:
        configured = str(session_state.get("gateway_proxy_mode") or session_state.get("proxy_mode") or "unknown")
        try:
            if protocol == "official":
                proxy_discovered = codex_proxy._official_proxy_url(request.full_url) is not None
            else:
                scheme = urlsplit(str(request.full_url)).scheme
                proxy_discovered = bool(codex_proxy.getproxies().get(scheme))
        except (AttributeError, OSError, ValueError):
            return "auto_proxy_unresolved" if configured == "auto_windows_registry" else "process_proxy_unresolved"
        if configured == "auto_windows_registry":
            return "auto_proxy_discovered" if proxy_discovered else "auto_direct"
        return "process_proxy_configured" if proxy_discovered else "process_direct"

    def finalize_request_trace(handler: Any, request_state: dict[str, Any] | None) -> None:
        if request_state is None or request_state.get("trace_finalized"):
            return
        request_state["trace_finalized"] = True
        if not isinstance(request_state.get("request"), str):
            return
        emit(
            "relay_exposure_summary",
            request_state,
            bytes_exposed=request_state["downstream_bytes_exposed"],
            lines_exposed=request_state["downstream_lines_exposed"],
            terminal_forwarded=request_state["downstream_terminal_forwarded"],
        )
        upstream_terminal = bool(request_state["upstream_sse_stats"].terminal_event_seen)
        if upstream_terminal and not request_state["downstream_terminal_forwarded"] and not request_state["downstream_write_failed"]:
            emit("gateway_relay_terminal_not_forwarded", request_state, failure_phase="relay_terminalization")
        close_requested = bool(getattr(handler, "close_connection", False))
        emit("gateway_local_socket_close_requested", request_state, close_requested=close_requested)

    record(
        {
            "event": "gateway_trace_started",
            "configured_proxy_mode": str(session_state.get("gateway_proxy_mode") or session_state.get("proxy_mode") or "unknown"),
            **_current_gateway_transport_scope(),
        }
    )

    original_write = codex_proxy.write_proxy_event

    def traced_write(event: str, **fields: Any) -> None:
        request_state = current_request_state()
        raw_request = fields.get("request_id")
        if request_state is not None:
            bind_request(request_state, raw_request)
        gateway_fields: dict[str, Any] = {"gateway_event": event}
        for name in GATEWAY_EVENT_FIELDS:
            value = _safe_event_value(fields.get(name))
            if value is None:
                continue
            if name == "failure_phase":
                gateway_fields["reported_failure_phase"] = value
            elif name == "failure_side":
                gateway_fields["reported_failure_side"] = value
            elif name == "failure_class":
                gateway_fields["reported_failure_class"] = value
            else:
                gateway_fields[name] = value
        emit("gateway_event", request_state, **gateway_fields)
        if request_state is not None and event == "upstream_retry":
            attempt = fields.get("attempt")
            max_attempts = fields.get("max_attempts")
            if isinstance(attempt, int) and not isinstance(attempt, bool):
                request_state["attempt"] = attempt + 1
            if isinstance(max_attempts, int) and not isinstance(max_attempts, bool):
                request_state["retry_budget"] = max_attempts
        if request_state is not None and event == "official_passthrough_stream_closed":
            relay_fields = {
                "failure_phase": _live_upstream_phase(request_state),
                "reported_failure_phase": _safe_event_value(fields.get("failure_phase")),
                "reported_failure_side": _safe_event_value(fields.get("failure_side")),
                "reported_failure_class": _safe_event_value(fields.get("failure_class")),
                "lines_streamed": _safe_event_value(fields.get("lines_streamed")),
                "bytes_streamed": _safe_event_value(fields.get("bytes_streamed")),
                "last_upstream_byte_age_ms": _safe_event_value(fields.get("last_upstream_byte_age_ms")),
                "synthetic_terminal_event_sent": _safe_event_value(fields.get("synthetic_terminal_event_sent")),
                "synthetic_terminal_event_type": _safe_event_value(fields.get("synthetic_terminal_event_type")),
                "client_disconnected": _safe_event_value(fields.get("client_disconnected")),
            }
            emit("relay_reported_stream_close", request_state, **relay_fields)
        original_write(event, **fields)

    codex_proxy.write_proxy_event = traced_write

    original_handle = codex_proxy.CodexProxyHandler.handle_one_request

    def traced_handle(handler: Any) -> Any:
        request_state = new_request_state(getattr(handler, "client_address", None))
        context.request_state = request_state
        context.last_request_state = None
        emit(
            "downstream_accept",
            request_state,
            desktop_route_mode=request_state["desktop_route_mode"],
            app_stream_consumer_boundary=request_state["app_stream_consumer_boundary"],
        )
        try:
            return original_handle(handler)
        finally:
            request_state["close_requested"] = bool(getattr(handler, "close_connection", False))
            if isinstance(request_state.get("request"), str):
                emit("downstream_handler_complete", request_state, close_requested=request_state["close_requested"])
            context.last_request_state = request_state
            context.request_state = None

    codex_proxy.CodexProxyHandler.handle_one_request = traced_handle

    original_finish = codex_proxy.CodexProxyHandler.finish

    def traced_finish(handler: Any) -> Any:
        request_state = current_request_state() or getattr(context, "last_request_state", None)
        try:
            return original_finish(handler)
        finally:
            if isinstance(request_state, dict) and isinstance(request_state.get("request"), str):
                emit(
                    "gateway_local_socket_closed",
                    request_state,
                    close_requested=bool(getattr(handler, "close_connection", False)),
                )
            context.last_request_state = None

    codex_proxy.CodexProxyHandler.finish = traced_finish

    original_parse = codex_proxy._parse_gateway_request_input

    def traced_parse(handler: Any, *args: Any, **kwargs: Any) -> Any:
        request_state = current_request_state()
        if request_state is None:
            return original_parse(handler, *args, **kwargs)
        bind_request(request_state, kwargs.get("request_id"))
        content_length = kwargs.get("content_length")
        expected_bytes = content_length if isinstance(content_length, int) and content_length >= 0 else 0
        original_reader = handler.rfile
        handler.rfile = _BoundaryTraceReader(
            original_reader,
            request_state=request_state,
            expected_bytes=expected_bytes,
            record=record,
        )
        try:
            return original_parse(handler, *args, **kwargs)
        finally:
            handler.rfile = original_reader

    codex_proxy._parse_gateway_request_input = traced_parse

    original_send_response = codex_proxy.CodexProxyHandler.send_response

    def traced_send_response(handler: Any, code: int, message: str | None = None) -> Any:
        request_state = current_request_state()
        if request_state is not None and isinstance(request_state.get("request"), str):
            request_state["downstream_headers_complete"] = False
            emit("downstream_response_open", request_state, status_class=_status_class(code))
        return original_send_response(handler, code, message)

    codex_proxy.CodexProxyHandler.send_response = traced_send_response

    original_end_headers = codex_proxy.CodexProxyHandler.end_headers

    def traced_end_headers(handler: Any) -> Any:
        result = original_end_headers(handler)
        request_state = current_request_state()
        if request_state is not None and isinstance(request_state.get("request"), str):
            request_state["downstream_headers_complete"] = True
            emit("downstream_response_headers_sent", request_state)
        return result

    codex_proxy.CodexProxyHandler.end_headers = traced_end_headers

    original_do_post = codex_proxy.CodexProxyHandler.do_POST

    def traced_do_post(handler: Any) -> Any:
        request_state = current_request_state()
        if request_state is None:
            return original_do_post(handler)
        original_writer = handler.wfile
        handler.wfile = _BoundaryTraceWriter(original_writer, request_state=request_state, record=record)
        try:
            return original_do_post(handler)
        except BaseException as exc:
            if request_state["upstream_sse_stats"].terminal_event_seen:
                emit(
                    "gateway_relay_exception_after_terminal",
                    request_state,
                    failure_phase="relay_terminalization",
                    error=type(exc).__name__,
                )
            raise
        finally:
            handler.wfile = original_writer
            finalize_request_trace(handler, request_state)

    codex_proxy.CodexProxyHandler.do_POST = traced_do_post

    original_open_response = codex_proxy._open_upstream_response

    def traced_open_response(*args: Any, **kwargs: Any) -> Any:
        request_state = current_request_state()
        if request_state is not None:
            request_kind = kwargs.get("request_kind")
            retry_budget = kwargs.get("max_attempts")
            if not isinstance(retry_budget, int) or isinstance(retry_budget, bool):
                try:
                    retry_budget = codex_proxy._upstream_retry_attempts(str(request_kind or "main_generation"))
                except (AttributeError, TypeError, ValueError):
                    retry_budget = None
            if isinstance(retry_budget, int) and not isinstance(retry_budget, bool):
                request_state["retry_budget"] = retry_budget
                emit("upstream_retry_budget", request_state, retry_budget=retry_budget, attempt=request_state["attempt"])
        return original_open_response(*args, **kwargs)

    codex_proxy._open_upstream_response = traced_open_response

    original_open_once = codex_proxy._open_upstream_once

    def traced_open_once(request: Any, *, upstream_name: str, timeout: int) -> Any:
        request_state = current_request_state()
        if request_state is None:
            return original_open_once(request, upstream_name=upstream_name, timeout=timeout)
        protocol = upstream_name if upstream_name in {"official", "external"} else "external"
        request_state["protocol"] = protocol
        request_state["last_upstream_phase"] = "unknown"
        opener_before = _stdlib_global_opener_state() if protocol == "official" else None
        emit(
            "upstream_attempt_begin",
            request_state,
            protocol=protocol,
            attempt=request_state["attempt"],
            retry_budget=request_state.get("retry_budget"),
            configured_proxy_mode=str(session_state.get("gateway_proxy_mode") or session_state.get("proxy_mode") or "unknown"),
            effective_proxy_mode=effective_proxy_mode(protocol, request),
            connection_disposition="unobserved",
            official_global_opener_before=opener_before,
        )
        try:
            response = original_open_once(request, upstream_name=upstream_name, timeout=timeout)
        except HTTPError as exc:
            request_state["last_upstream_phase"] = "response_headers"
            emit(
                "upstream_service_error_headers",
                request_state,
                protocol=protocol,
                status_class=_status_class(getattr(exc, "code", None)),
                official_global_opener_after=_stdlib_global_opener_state() if protocol == "official" else None,
                official_global_opener_changed=(
                    opener_before != _stdlib_global_opener_state() if protocol == "official" else False
                ),
            )
            raise
        except BaseException as exc:
            request_state["upstream_failure_observed"] = True
            emit(
                "upstream_open_failed",
                request_state,
                protocol=protocol,
                failure_phase=_live_upstream_phase(request_state),
                error=type(exc).__name__,
                official_global_opener_after=_stdlib_global_opener_state() if protocol == "official" else None,
                official_global_opener_changed=(
                    opener_before != _stdlib_global_opener_state() if protocol == "official" else False
                ),
            )
            raise
        request_state["last_upstream_phase"] = "response_headers"
        connection = _response_connection_identity(response)
        connection_label: str | None = None
        disposition = "unobserved"
        if connection is not None:
            connection_label = _opaque_label(session_state, "upstream_connection", id(connection))
            with connection_lock:
                disposition = "reused" if connection_label in observed_connections else "new"
                observed_connections.add(connection_label)
        status = getattr(response, "status", getattr(response, "code", None))
        emit(
            "upstream_response_headers",
            request_state,
            protocol=protocol,
            status_class=_status_class(status),
            upstream_connection=connection_label,
            connection_disposition=disposition,
            official_global_opener_after=_stdlib_global_opener_state() if protocol == "official" else None,
            official_global_opener_changed=(
                opener_before != _stdlib_global_opener_state() if protocol == "official" else False
            ),
        )
        return _wrap_readline(response, record=record, request_state=request_state, protocol=protocol)

    codex_proxy._open_upstream_once = traced_open_once

    def trace_network_phase(
        begin_event: str,
        success_event: str,
        failure_event: str,
        phase: str,
        original: Callable[..., Any],
    ) -> Callable[..., Any]:
        def traced_network_call(*args: Any, **kwargs: Any) -> Any:
            request_state = current_request_state()
            if request_state is None:
                return original(*args, **kwargs)
            request_state["last_upstream_phase"] = phase
            emit(begin_event, request_state, failure_phase=phase)
            try:
                value = original(*args, **kwargs)
            except BaseException as exc:
                request_state["upstream_failure_observed"] = True
                emit(failure_event, request_state, failure_phase=phase, error=type(exc).__name__)
                raise
            emit(success_event, request_state, failure_phase=phase)
            return value

        return traced_network_call

    socket.getaddrinfo = trace_network_phase(
        "upstream_dns_begin", "upstream_dns_succeeded", "upstream_dns_failed", "dns", socket.getaddrinfo
    )

    def trace_connection_factory(original: Callable[..., Any]) -> Callable[..., Any]:
        def traced_connection_factory(*args: Any, **kwargs: Any) -> Any:
            request_state = current_request_state()
            if request_state is None:
                return original(*args, **kwargs)
            request_state["last_upstream_phase"] = "tcp_connect"
            emit("upstream_tcp_connect_begin", request_state, failure_phase="tcp_connect")
            try:
                connection = original(*args, **kwargs)
            except BaseException as exc:
                emit("upstream_tcp_connect_failed", request_state, failure_phase="tcp_connect", error=type(exc).__name__)
                raise
            emit(
                "upstream_tcp_connect_succeeded",
                request_state,
                failure_phase="tcp_connect",
                upstream_connection=_opaque_label(session_state, "upstream_connection", id(connection)),
                connection_disposition="new",
            )
            return connection

        return traced_connection_factory

    socket.create_connection = trace_connection_factory(socket.create_connection)
    codex_proxy.urllib3.util.connection.create_connection = trace_connection_factory(
        codex_proxy.urllib3.util.connection.create_connection
    )
    ssl.SSLSocket.do_handshake = trace_network_phase(
        "upstream_tls_handshake_begin",
        "upstream_tls_handshake_succeeded",
        "upstream_tls_handshake_failed",
        "tls_handshake",
        ssl.SSLSocket.do_handshake,
    )
    original_send = http.client.HTTPConnection.send

    def traced_send(connection: Any, data: Any) -> Any:
        request_state = current_request_state()
        if request_state is None:
            return original_send(connection, data)
        request_state["last_upstream_phase"] = "request_write"
        byte_count = len(data) if isinstance(data, (bytes, bytearray, memoryview)) else 0
        emit("upstream_request_write_begin", request_state, failure_phase="request_write", bytes=byte_count)
        try:
            value = original_send(connection, data)
        except BaseException as exc:
            request_state["upstream_failure_observed"] = True
            emit(
                "upstream_request_write_failed",
                request_state,
                failure_phase="request_write",
                error=type(exc).__name__,
                bytes=byte_count,
            )
            raise
        emit("upstream_request_write_succeeded", request_state, failure_phase="request_write", bytes=byte_count)
        return value

    http.client.HTTPConnection.send = traced_send

    original_official_relay = codex_proxy.CodexProxyHandler._relay_official_passthrough_sse_response

    def traced_official_relay(handler: Any, response: Any, upstream_name: str, *args: Any, **kwargs: Any) -> Any:
        request_state = current_request_state()
        if request_state is None:
            return original_official_relay(handler, response, upstream_name, *args, **kwargs)
        bind_request(request_state, kwargs.get("request_id"))
        emit("relay_official_passthrough_begin", request_state, protocol="official")
        try:
            status = original_official_relay(handler, response, upstream_name, *args, **kwargs)
        except BaseException as exc:
            if request_state["upstream_sse_stats"].terminal_event_seen:
                emit(
                    "gateway_relay_exception_after_terminal",
                    request_state,
                    failure_phase="relay_terminalization",
                    error=type(exc).__name__,
                )
            raise
        emit("relay_official_passthrough_end", request_state, status_class=_status_class(status))
        return status

    codex_proxy.CodexProxyHandler._relay_official_passthrough_sse_response = traced_official_relay


def _gateway(session_id: str) -> int:
    root = _session_root(session_id)
    state = _load_state(session_id)
    port = state.get("gateway_port")
    if not isinstance(port, int):
        return 2
    _install_gateway_trace(root, state)
    import codex_proxy

    codex_proxy.run_server("127.0.0.1", port)
    return 0


def cli_control(session_id: str) -> dict[str, Any]:
    root = _session_root(session_id)
    state = _load_state(session_id)
    if not _pid_alive(state.get("gateway_pid")):
        raise CaptureError("gateway_control_is_not_running")
    paths = _runtime_paths(root)
    codex = resolve_app_codex(None)
    command = [
        sys.executable,
        str(SCRIPTS_ROOT / "e2e_codex_app_transport.py"),
        "--codex",
        str(codex),
        "--home",
        str(paths["codex_home"]),
        "--cwd",
        str(REPOSITORY_ROOT),
        "--model",
        str(state.get("model") or DEFAULT_MODEL),
        "--model-provider",
        "custom",
        "--turns",
        "1",
        "--timeout",
        "90",
        "--input-bytes",
        str(DEFAULT_PROMPT_BYTES),
    ]
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=_manual_child_environment(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
        timeout=150,
    )
    turns = 0
    maximum: float | None = None
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get("event") == "turn_completed":
            turns += 1
        if value.get("event") == "probe_completed" and isinstance(value.get("max_duration_seconds"), (int, float)):
            maximum = float(value["max_duration_seconds"])
    result = {
        "status": "passed" if completed.returncode == 0 and turns == 1 else "failed",
        "completed_turns": turns,
        "max_duration_seconds": maximum,
    }
    state["cli_negative_control"] = result
    _write_state(root, state)
    return {"session": session_id, "cli_negative_control": result}


def dry_run() -> dict[str, Any]:
    seams = _desktop_app_seams()
    return {
        "status": "ready_for_manual_preparation"
        if seams.installed and seams.electron_user_data_override and seams.codex_home_override
        else "desktop_isolation_seam_unavailable",
        "desktop_app": asdict(seams),
        "next_localization_leg": {
            "variable": "Gateway insertion on the known-fault Official Desktop path",
            "route": "gateway_official_auto",
            "direct_official": "historical_reporter_control_not_rerun",
            "model": DEFAULT_MODEL,
            "proxy_mode": "auto_windows_registry",
            "websockets": "disabled_for_gateway_leg",
            "gateway_retries": "disabled_for_first_failure_capture",
            "clean_window_policy": "stop_without_retest_and_rearm_a_new_disposable_gateway_session_only_on_natural_recurrence",
        },
        "authentication": {
            "bootstrap_leg": AUTH_BOOTSTRAP_LEG,
            "rule": "manual_disposable_login_only_no_model_request_or_renderer_result",
        },
        "artifacts": [
            "sanitized_app_renderer_result",
            "app_server_lifecycle_labels",
            "gateway_request_and_connection_labels",
            "gateway_failure_phase_and_terminal_outcome",
            "first_closing_side_classification",
        ],
        "launcher_readiness": {
            "command": "launch-readiness",
            "strategies": list(LAUNCH_READINESS_STRATEGIES),
            "default_launcher_strategy": DEFAULT_LAUNCHER_STRATEGY,
            "probe_output": "sanitized_exit_category_and_lifecycle_only",
        },
        "guardrails": [
            "no_computer_use_or_ui_automation",
            "no_shared_profile_or_database_edits",
            "no_credential_copying",
            "no_global_proxy_or_tun_changes",
            "no_broad_process_termination",
        ],
    }


def _render(result: dict[str, Any]) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--model", default=DEFAULT_MODEL)
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("--session", required=True)
    launch_parser.add_argument("--leg", choices=LEGS, required=True)
    launch_parser.add_argument(
        "--launcher-strategy",
        choices=LAUNCH_READINESS_STRATEGIES,
        default=DEFAULT_LAUNCHER_STRATEGY,
    )
    readiness_parser = subparsers.add_parser("launch-readiness")
    readiness_parser.add_argument("--strategy", choices=("all", *LAUNCH_READINESS_STRATEGIES), default="all")
    readiness_parser.add_argument("--duration-seconds", type=float, default=DEFAULT_LAUNCH_READINESS_SECONDS)
    arm_long_stream_parser = subparsers.add_parser("arm-long-stream")
    arm_long_stream_parser.add_argument("--session", required=True)
    long_stream_marker_parser = subparsers.add_parser("long-stream-marker")
    long_stream_marker_parser.add_argument("--session", required=True)
    long_stream_marker_parser.add_argument("--marker", choices=LONG_STREAM_MARKERS, required=True)
    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("--session", required=True)
    mark_parser.add_argument("--result", choices=APP_RESULTS, required=True)
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--session", required=True)
    abort_parser = subparsers.add_parser("abort")
    abort_parser.add_argument("--session", required=True)
    control_parser = subparsers.add_parser("cli-control")
    control_parser.add_argument("--session", required=True)
    gateway_parser = subparsers.add_parser("_gateway")
    gateway_parser.add_argument("--session", required=True)
    watch_parser = subparsers.add_parser("_watch")
    watch_parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            _render(dry_run())
            return 0
        if args.command == "prepare":
            _render(prepare(args.model))
            return 0
        if args.command == "launch":
            _render(launch(args.session, args.leg, launcher_strategy=args.launcher_strategy))
            return 0
        if args.command == "launch-readiness":
            _render(launch_readiness(args.strategy, args.duration_seconds))
            return 0
        if args.command == "arm-long-stream":
            _render(arm_long_stream(args.session))
            return 0
        if args.command == "long-stream-marker":
            _render(mark_long_stream(args.session, args.marker))
            return 0
        if args.command == "mark":
            _render(mark(args.session, args.result))
            return 0
        if args.command == "collect":
            _render(collect(args.session))
            return 0
        if args.command == "abort":
            _render(collect(args.session, aborted=True))
            return 0
        if args.command == "cli-control":
            _render(cli_control(args.session))
            return 0
        if args.command == "_gateway":
            return _gateway(args.session)
        if args.command == "_watch":
            return _watch(args.session)
        parser.print_help()
        return 2
    except CaptureError as exc:
        _render({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
