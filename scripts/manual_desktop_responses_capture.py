"""Run a bounded, human-assisted Desktop App capture for Issue #114.

This runner deliberately has no renderer driver.  It creates one disposable
Desktop profile, launches the installed Desktop App only when a maintainer
explicitly asks it to, and collects sanitized lifecycle/Gateway evidence after
the maintainer finishes the visible turn.  It never reads or copies a shared
profile, credential, proxy configuration, or request body.

The first A/B changes exactly one variable: direct Official traffic versus the
same Desktop build/profile/model through an isolated Gateway using automatic
Windows proxy discovery.  Gateway retries are disabled only to expose the first
failure boundary; WebSockets remain disabled for the Gateway condition.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable
from urllib.error import URLError
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
LEGS = ("direct_official", "gateway_official_auto")
APP_RESULTS = (
    "completed",
    "reconnecting",
    "stream_disconnected",
    "duplicate_request_visible",
    "timed_out",
    "auth_required",
    "aborted",
)
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
    "duration_ms",
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


def launch(session_id: str, leg: str, *, launcher_strategy: str = DEFAULT_LAUNCHER_STRATEGY) -> dict[str, Any]:
    if leg not in LEGS:
        raise CaptureError("unsupported_leg")
    if launcher_strategy not in LAUNCH_READINESS_STRATEGIES:
        raise CaptureError("unsupported_launcher_strategy")
    root = _session_root(session_id)
    state = _load_state(session_id)
    if _pid_alive(state.get("current_app_pid")):
        raise CaptureError("previous_isolated_desktop_instance_is_still_running")
    if leg == "gateway_official_auto":
        paths = _runtime_paths(root)
        if not (paths["codex_home"] / "auth.json").is_file():
            raise CaptureError("manual_login_required_in_disposable_profile")
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
        "route": "gateway_official_auto" if leg == "gateway_official_auto" else "direct_official",
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
    entry = {"at": _utc_now(), "leg": leg, "result": result}
    state.setdefault("app_results", []).append(entry)
    _write_state(root, state)
    _append_jsonl(_capture_path(root, "app-cues.jsonl"), {"event": "renderer_terminal_cue", **entry})
    return {"status": "marked", "session": session_id, "leg": leg, "result": result}


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


def _summarize_gateway_events(state: dict[str, Any], events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    labels: dict[str, str] = {}
    event_rows: list[dict[str, Any]] = []
    first_closing_side = "not_observed"
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
        failure_side = item.get("failure_side")
        if first_closing_side == "not_observed":
            if event == "client_write_failed" or failure_side == "downstream_write":
                first_closing_side = "downstream_app_or_client"
            elif event in {"upstream_retry", "official_passthrough_stream_closed", "request_error"}:
                first_closing_side = "gateway_or_upstream"
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
        "first_closing_side": first_closing_side,
        "silent_terminal_request_count": sum(
            1 for label in labels.values() if label not in terminal_by_request
        ),
    }


def _summarize_trace(trace: Iterable[dict[str, Any]]) -> dict[str, Any]:
    connections: dict[str, list[str]] = defaultdict(list)
    opens: list[dict[str, Any]] = []
    reads: list[dict[str, Any]] = []
    for item in trace:
        event = item.get("event")
        request = item.get("request")
        if event == "upstream_opened" and isinstance(request, str):
            label = item.get("upstream_connection")
            if isinstance(label, str):
                connections[request].append(label)
            opens.append(
                {
                    key: item[key]
                    for key in ("event", "request", "protocol", "upstream_connection")
                    if isinstance(item.get(key), str)
                }
            )
        elif event in {"upstream_open_failed", "upstream_read_failed"}:
            reads.append(
                {
                    key: item[key]
                    for key in ("event", "request", "protocol", "error")
                    if isinstance(item.get(key), str)
                }
            )
    return {"connections_by_request": dict(connections), "opens": opens, "read_faults": reads}


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


def collect(session_id: str, *, aborted: bool = False) -> dict[str, Any]:
    root = _session_root(session_id)
    state = _load_state(session_id)
    if _pid_alive(state.get("current_app_pid")):
        raise CaptureError("close_isolated_desktop_before_collection")
    paths = _runtime_paths(root)
    paths["watch_stop"].parent.mkdir(parents=True, exist_ok=True)
    paths["watch_stop"].touch()
    gateway_stop = _manual_stop_gateway(root, state)
    overlay_restore = _restore_isolated_overlay(root, state)
    state["gateway_stop"] = gateway_stop
    state["overlay_restore"] = overlay_restore
    state["collected_at"] = _utc_now()
    _write_state(root, state)
    report = {
        "schema_version": 1,
        "status": "aborted" if aborted else "collected",
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
        "gateway": _summarize_gateway_events(state, _read_jsonl(paths["gateway_events"])),
        "gateway_connection_trace": _summarize_trace(_read_jsonl(_capture_path(root, "gateway-trace.jsonl"))),
        "cleanup": {
            "gateway": gateway_stop,
            "isolated_config_overlay": overlay_restore,
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


def _response_connection_identity(response: Any) -> object:
    raw_response = getattr(response, "_response", response)
    connection = getattr(raw_response, "connection", None)
    socket_value = getattr(connection, "sock", None)
    return socket_value if socket_value is not None else connection if connection is not None else raw_response


def _wrap_readline(
    response: Any,
    *,
    record: Callable[[dict[str, Any]], None],
    request_label: str | None,
    protocol: str,
) -> Any:
    original = getattr(response, "readline", None)
    if not callable(original):
        return response

    def traced_readline(*args: Any, **kwargs: Any) -> Any:
        try:
            return original(*args, **kwargs)
        except BaseException as exc:
            record(
                {
                    "at": _utc_now(),
                    "event": "upstream_read_failed",
                    "request": request_label,
                    "protocol": protocol,
                    "error": type(exc).__name__,
                }
            )
            raise

    try:
        setattr(response, "readline", traced_readline)
    except (AttributeError, TypeError):
        pass
    return response


def _install_gateway_trace(root: Path, state: dict[str, Any]) -> None:
    import codex_proxy

    trace_path = _capture_path(root, "gateway-trace.jsonl")
    trace_lock = threading.Lock()
    context = threading.local()

    def record(payload: dict[str, Any]) -> None:
        compact = {key: value for key, value in payload.items() if value is not None}
        _append_jsonl(trace_path, compact, lock=trace_lock)

    original_write = codex_proxy.write_proxy_event

    def traced_write(event: str, **fields: Any) -> None:
        raw_request = fields.get("request_id")
        if event == "request_start" and isinstance(raw_request, str):
            context.request_id = raw_request
        request_label = (
            _opaque_label(state, "gateway_request", raw_request)
            if isinstance(raw_request, str) and raw_request
            else None
        )
        payload: dict[str, Any] = {
            "at": _utc_now(),
            "event": "gateway_event",
            "gateway_event": event,
            "request": request_label,
            "downstream_connection": getattr(context, "downstream_connection", None),
        }
        for name in GATEWAY_EVENT_FIELDS:
            value = _safe_event_value(fields.get(name))
            if value is not None:
                payload[name] = value
        record(payload)
        original_write(event, **fields)

    codex_proxy.write_proxy_event = traced_write

    original_handle = codex_proxy.CodexProxyHandler.handle_one_request

    def traced_handle(handler: Any) -> Any:
        peer = getattr(handler, "client_address", None)
        context.downstream_connection = _opaque_label(state, "downstream_connection", peer)
        record(
            {
                "at": _utc_now(),
                "event": "downstream_connection_opened",
                "downstream_connection": context.downstream_connection,
            }
        )
        return original_handle(handler)

    codex_proxy.CodexProxyHandler.handle_one_request = traced_handle

    def instrument_open(protocol: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def traced_open(*args: Any, **kwargs: Any) -> Any:
            raw_request = getattr(context, "request_id", None)
            request_label = (
                _opaque_label(state, "gateway_request", raw_request)
                if isinstance(raw_request, str) and raw_request
                else None
            )
            try:
                response = original(*args, **kwargs)
            except BaseException as exc:
                record(
                    {
                        "at": _utc_now(),
                        "event": "upstream_open_failed",
                        "request": request_label,
                        "protocol": protocol,
                        "error": type(exc).__name__,
                    }
                )
                raise
            connection = _response_connection_identity(response)
            record(
                {
                    "at": _utc_now(),
                    "event": "upstream_opened",
                    "request": request_label,
                    "protocol": protocol,
                    "upstream_connection": _opaque_label(state, "upstream_connection", id(connection)),
                }
            )
            return _wrap_readline(response, record=record, request_label=request_label, protocol=protocol)

        return traced_open

    codex_proxy._official_urlopen = instrument_open("official", codex_proxy._official_urlopen)
    codex_proxy.urlopen = instrument_open("external", codex_proxy.urlopen)


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
        "first_ab": {
            "variable": "direct_official_vs_gateway_official_auto",
            "model": DEFAULT_MODEL,
            "proxy_mode": "auto_windows_registry",
            "websockets": "disabled_for_gateway_leg",
            "gateway_retries": "disabled_for_first_failure_capture",
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
