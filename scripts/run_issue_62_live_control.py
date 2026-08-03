"""Run the bounded, sanitized orchestration envelope for Issue #62.

This module deliberately separates orchestration from the real upstream
controls.  The default command-line mode remains ``--fixture`` and therefore
cannot qualify Issue #62.  An explicitly enabled ``--enable-live-control``
mode accepts a sanitized, operator-supplied plan, binds its candidate/catalog/
route files, runs real CLI and sidecar commands through
:class:`BoundedPhaseRunner`, and still leaves qualification closed for
independent review.

The harness has three non-negotiable properties:

* every phase emits a small JSONL status marker before and after it runs;
* subprocesses have a 30--60 second deadline, never persist stdout/stderr, and
  are terminated on timeout/cancellation; and
* cleanup always runs and writes a receipt.  Any failure (including cleanup or
  journal failure) leaves ``ready_for_issue62`` false.

Only candidate identity, digests, bounded counts, exit status, and allow-listed
error codes are retained.  Prompts, bodies, headers, command arguments,
process IDs, paths, credentials, and exception text are intentionally absent
from all artifacts.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Literal, Mapping, NoReturn, Sequence


SCHEMA_VERSION = "codexhub.issue62.live-control.v1"
MIN_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 30.0
ALLOWED_ERROR_CODES = frozenset(
    {
        "cancelled",
        "cleanup_failed",
        "cleanup_incomplete",
        "cleanup_timeout",
        "exit_nonzero",
        "journal_write_failed",
        "phase_started",
        "phase_exception",
        "phase_timeout",
        "process_start_failed",
        "stale_artifact",
        "termination_failed",
        # Live-plan validation codes are intentionally fixed and safe to
        # expose in the operator result.  They never include paths, command
        # arguments, or exception text.
        "live_control_plan_missing",
        "live_control_plan_invalid",
        "candidate_binding_mismatch",
        "plan_path_invalid",
        "plan_path_outside_isolation",
        "plan_path_linked",
        "environment_invalid",
        "cli_executable_binding_mismatch",
        "helper_executable_binding_mismatch",
        "planner_plan_incomplete",
        "planner_disposition_invalid",
        "candidate_sha_binding_mismatch",
        "cli_package_binding_mismatch",
        "catalog_binding_mismatch",
        "route_binding_mismatch",
        "control_labels_incomplete",
        "sidecar_capture_missing",
        "sidecar_capture_incomplete",
        "identity_replay_incomplete",
        "identity_replay_artifact_missing",
        "identity_replay_artifact_invalid",
        "manifest_reconcile_failed",
    }
)

LIVE_PLAN_SCHEMA = "codexhub.issue62.live-control-plan.v1"
LIVE_SCOPE = "authorized_live_control"
CONTROL_NAMES = (
    "streaming_text",
    "streaming_function_history",
    "non_streaming_text",
    "choice_auto",
    "choice_none",
    "terminal_success",
    "terminal_error",
    "error_json",
)
CONTROL_NAME_SET = frozenset(CONTROL_NAMES)
LIVE_PLAN_FIELDS = frozenset(
    {
        "schema",
        "verification_scope",
        "isolation_root",
        "candidate_identity",
        "binding",
        "catalog_model_entry_id",
        "cli",
        "sidecars",
        "controls",
        "replays",
        "environment",
        "planner",
    }
)
LIVE_DISPOSITIONS = frozenset({"Preserved", "Unsupported", "Unqualified"})
LIVE_ENV_KEYS = frozenset(
    {"PATH", "SystemRoot", "ComSpec", "TEMP", "TMP", "PATHEXT", "PYTHONPATH"}
)
LOCAL_ENV_PATH_KEYS = frozenset({"PATH", "TEMP", "TMP", "PYTHONPATH"})
SENSITIVE_ENV_KEYS = frozenset(
    {
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "OLLAMA_API_KEY",
        "CODEX_AUTH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
    }
)
PLANNER_FIELDS = frozenset(
    {"model_visible_plan", "hosted_only_disposition", "unknown_tag_disposition"}
)


class LiveControlValidationError(ValueError):
    """A deterministic, path-free live-control plan validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code if code in ALLOWED_ERROR_CODES else "live_control_plan_invalid"
        super().__init__(self.code)


class HarnessFailure(RuntimeError):
    """An internal failure represented by a safe, allow-listed code."""

    def __init__(self, code: str) -> None:
        if code not in ALLOWED_ERROR_CODES:
            code = "phase_exception"
        super().__init__(code)
        self.code = code


def _safe_code(value: str) -> str:
    if value in ALLOWED_ERROR_CODES:
        return value
    return "phase_exception"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validate_hex(value: str, *, lengths: tuple[int, ...]) -> str:
    if not isinstance(value, str) or len(value) not in lengths:
        raise ValueError("invalid digest")
    if re.fullmatch(r"[0-9a-fA-F]+", value) is None:
        raise ValueError("invalid digest")
    return value.lower()


def _validate_cli_version(value: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", value) is None:
        raise ValueError("invalid cli version")
    return value


@dataclass(frozen=True)
class CandidateIdentity:
    """The only identity data allowed in a live-control artifact."""

    candidate_sha: str
    cli_version: str
    cli_package_sha256: str
    catalog_digest: str
    route_digest: str
    cli_source_commit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_sha", _validate_hex(self.candidate_sha, lengths=(40, 64))
        )
        object.__setattr__(self, "cli_version", _validate_cli_version(self.cli_version))
        object.__setattr__(
            self,
            "cli_package_sha256",
            _validate_hex(self.cli_package_sha256, lengths=(64,)),
        )
        object.__setattr__(
            self, "catalog_digest", _validate_hex(self.catalog_digest, lengths=(64,))
        )
        object.__setattr__(
            self, "route_digest", _validate_hex(self.route_digest, lengths=(64,))
        )
        if self.cli_source_commit is not None:
            object.__setattr__(
                self,
                "cli_source_commit",
                _validate_hex(self.cli_source_commit, lengths=(40, 64)),
            )

    def as_dict(self) -> dict[str, str]:
        result = {
            "candidate_sha": self.candidate_sha,
            "cli_version": self.cli_version,
            "cli_package_sha256": self.cli_package_sha256,
            "catalog_digest": self.catalog_digest,
            "route_digest": self.route_digest,
        }
        if self.cli_source_commit is not None:
            result["cli_source_commit"] = self.cli_source_commit
        return result


@dataclass(frozen=True)
class CommandResult:
    status: Literal["completed", "failed", "timed_out", "cancelled"]
    status_code: str
    exit_code: int | None = None
    terminated: bool = True


@dataclass(frozen=True)
class BackgroundProcess:
    """A bounded child kept alive while a control command executes."""

    phase: str
    process: subprocess.Popen[bytes]


def _validate_timeout(timeout_seconds: float) -> float:
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be between 30 and 60 seconds")
    return float(timeout_seconds)


ALLOWED_ARTIFACT_NAMES = frozenset({"phase-status.jsonl", "cleanup-receipt.json"})
MAX_NESTED_ARTIFACT_SCAN = 2048
CleanupAction = Callable[[], None]


def _ensure_fresh_run_root(run_root: Path) -> None:
    """Reject reused roots before any evidence is written."""

    try:
        if run_root.exists():
            if run_root.is_symlink() or not run_root.is_dir() or any(run_root.iterdir()):
                raise HarnessFailure("stale_artifact")
        else:
            run_root.mkdir(parents=True, exist_ok=False)
    except HarnessFailure:
        raise
    except OSError as error:
        raise HarnessFailure("journal_write_failed") from error


def _count_extra_artifacts(run_root: Path) -> int:
    try:
        extras = 0
        for entry in run_root.iterdir():
            if (
                entry.name in ALLOWED_ARTIFACT_NAMES
                and entry.is_file()
                and not entry.is_symlink()
                and os.stat(entry).st_nlink == 1
            ):
                continue
            extras += 1
            if entry.is_dir() and not entry.is_symlink():
                # Traverse recursively so nested raw/temporary material is
                # detected, while counting one contaminated artifact tree.
                for index, _nested in enumerate(entry.rglob("*"), start=1):
                    if index >= MAX_NESTED_ARTIFACT_SCAN:
                        break
        return extras
    except OSError:
        return 1


def _write_sanitized_json(target: Path, payload: dict[str, object]) -> None:
    """Atomically write one allow-listed artifact without exposing a path."""

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=target.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except OSError as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise HarnessFailure("journal_write_failed") from error


def _terminate_process(process: subprocess.Popen[bytes]) -> bool:
    try:
        if process.poll() is not None:
            # A completed parent can still have a descendant.  Treat that
            # state as unknown rather than claiming the process tree is gone.
            return False
        if os.name == "nt":
            # ``terminate`` only signals the direct child on Windows.  The
            # Gateway/CLI can spawn grandchildren, so use taskkill's tree mode
            # and suppress all command output.
            kill_result = subprocess.run(
                ["taskkill", "/PID", str(int(process.pid)), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if kill_result.returncode != 0:
                raise OSError("taskkill failed")
        else:
            os.killpg(int(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
        return True
    except Exception:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(int(process.pid), signal.SIGKILL)
            process.wait(timeout=5)
            return True
        except Exception:
            return False


class BoundedPhaseRunner:
    """Public seam for bounded subprocess phases and cleanup.

    A run root is one-shot.  Existing or later extra artifacts are rejected so
    a stale capture cannot be mistaken for evidence from this candidate.
    """

    def __init__(
        self,
        run_root: Path,
        identity: CandidateIdentity,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        process_factory: Callable[..., subprocess.Popen[bytes]] | None = None,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
    ) -> None:
        _ensure_fresh_run_root(run_root)
        self.run_root = run_root
        self.identity = identity
        self.timeout_seconds = _validate_timeout(timeout_seconds)
        self._process_factory = process_factory or subprocess.Popen
        self._working_directory = str(working_directory) if working_directory is not None else None
        self._environment = _safe_environment(
            environment or {},
            working_directory=Path(working_directory)
            if working_directory is not None
            else None,
        )
        self.journal = SanitizedPhaseJournal(run_root, identity)
        self._active_process: subprocess.Popen[bytes] | None = None
        self._active_lock = threading.Lock()
        self._background_processes: dict[str, BackgroundProcess] = {}
        self._background_lock = threading.Lock()
        self._cancel_requested = False
        self._cleanup_called = False
        self._children_terminated = True

    @staticmethod
    def _phase_name(phase: str) -> str:
        if re.fullmatch(r"[a-z0-9_]{1,48}", phase) is None:
            raise HarnessFailure("phase_exception")
        return phase

    def _set_active(self, process: subprocess.Popen[bytes] | None) -> None:
        with self._active_lock:
            self._active_process = process

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> bool:
        return bool(argv) and all(
            isinstance(item, str) and bool(item) and "\x00" not in item for item in argv
        )

    def start_background(self, phase: str, argv: Sequence[str]) -> BackgroundProcess:
        """Start one bounded sidecar process without retaining its output."""

        phase_name = self._phase_name(phase)
        self.journal.append(
            marker=f"{phase_name}_started", status="started", status_code="phase_started"
        )
        if not self._validate_argv(argv):
            self.journal.append(
                marker=f"{phase_name}_completed",
                status="failed",
                status_code="process_start_failed",
            )
            raise HarnessFailure("process_start_failed")
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            start_new_session = True
        try:
            process = self._process_factory(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._environment,
                cwd=self._working_directory,
                close_fds=True,
                creationflags=creationflags,
                start_new_session=start_new_session,
                shell=False,
            )
        except Exception as error:
            self.journal.append(
                marker=f"{phase_name}_completed",
                status="failed",
                status_code="process_start_failed",
            )
            raise HarnessFailure("process_start_failed") from error
        handle = BackgroundProcess(phase_name, process)
        with self._background_lock:
            if phase_name in self._background_processes:
                # A duplicate phase would make cleanup ambiguous.  Terminate
                # the just-started process before failing closed.
                _terminate_process(process)
                raise HarnessFailure("phase_exception")
            self._background_processes[phase_name] = handle
        return handle

    def stop_background(self, handle: BackgroundProcess, *, status_code: str = "cancelled") -> bool:
        """Stop one background child and emit its terminal journal marker."""

        with self._background_lock:
            current = self._background_processes.pop(handle.phase, None)
        if current is None:
            return True
        started = time.monotonic()
        terminated = _terminate_process(current.process)
        self._children_terminated = self._children_terminated and terminated
        code = status_code if terminated else "termination_failed"
        self.journal.append(
            marker=f"{current.phase}_completed",
            status="completed" if terminated else "failed",
            status_code=code,
            duration_ms=int((time.monotonic() - started) * 1000),
            exit_code=(int(current.process.poll()) if current.process.poll() is not None else None),
        )
        return terminated

    def _stop_background_processes(self) -> int:
        with self._background_lock:
            handles = list(self._background_processes.values())
        failures = 0
        for handle in handles:
            if not self.stop_background(handle):
                failures += 1
        return failures

    def mark_phase(
        self,
        marker: str,
        *,
        status: Literal["started", "completed", "failed", "cancelled"] = "completed",
        status_code: str = "ok",
        duration_ms: int | None = 0,
        counts: dict[str, int] | None = None,
    ) -> None:
        """Record a non-subprocess phase without accepting operational data."""

        self._phase_name(marker)
        self.journal.append(
            marker=marker,
            status=status,
            status_code=status_code,
            duration_ms=duration_ms,
            counts=counts,
        )

    def run_subprocess(
        self,
        phase: str,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> CommandResult:
        """Run one subprocess and always emit a terminal phase marker."""

        phase_name = self._phase_name(phase)
        timeout = _validate_timeout(
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        started = time.monotonic()
        self.journal.append(
            marker=f"{phase_name}_started", status="started", status_code="phase_started"
        )

        def finish(result: CommandResult) -> CommandResult:
            self._children_terminated = self._children_terminated and result.terminated
            self.journal.append(
                marker=f"{phase_name}_completed",
                status=(
                    "completed"
                    if result.status == "completed"
                    else "cancelled"
                    if result.status == "cancelled"
                    else "failed"
                ),
                status_code=result.status_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                exit_code=result.exit_code,
            )
            return result

        if cancellation_event is not None and cancellation_event.is_set():
            return finish(CommandResult("cancelled", "cancelled", terminated=True))
        if self._cancel_requested:
            return finish(CommandResult("cancelled", "cancelled", terminated=True))
        if not self._validate_argv(argv):
            return finish(CommandResult("failed", "process_start_failed", terminated=True))

        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            start_new_session = True
        try:
            process = self._process_factory(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._environment,
                cwd=self._working_directory,
                close_fds=True,
                creationflags=creationflags,
                start_new_session=start_new_session,
                shell=False,
            )
        except Exception:
            return finish(CommandResult("failed", "process_start_failed", terminated=True))

        self._set_active(process)
        try:
            deadline = time.monotonic() + timeout
            while True:
                return_code = process.poll()
                if return_code is not None:
                    result = (
                        CommandResult("completed", "ok", exit_code=0, terminated=True)
                        if return_code == 0
                        else CommandResult(
                            "failed",
                            "exit_nonzero",
                            exit_code=int(return_code),
                            terminated=True,
                        )
                    )
                    break
                if (cancellation_event is not None and cancellation_event.is_set()) or self._cancel_requested:
                    terminated = self.cancel()
                    result = CommandResult(
                        "cancelled",
                        "cancelled" if terminated else "termination_failed",
                        terminated=terminated,
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminated = self.cancel()
                    result = CommandResult(
                        "timed_out",
                        "phase_timeout" if terminated else "termination_failed",
                        terminated=terminated,
                    )
                    break
                try:
                    process.wait(timeout=min(0.2, remaining))
                except (subprocess.TimeoutExpired, TimeoutError):
                    continue
                except Exception:
                    terminated = self.cancel()
                    result = CommandResult(
                        "failed", "termination_failed", terminated=terminated
                    )
                    break
        finally:
            self._set_active(None)
        return finish(result)

    def cancel(self) -> bool:
        """Terminate the active process tree; false means it remains alive."""

        self._cancel_requested = True
        with self._active_lock:
            process = self._active_process
        if process is None:
            return True
        terminated = _terminate_process(process)
        self._children_terminated = self._children_terminated and terminated
        return terminated

    def cleanup(self, actions: Sequence[CleanupAction] = ()) -> dict[str, object]:
        """Stop children, reject extra artifacts, and emit a sanitized receipt."""

        if self._cleanup_called:
            return {
                "cleanup_attempted": True,
                "cleanup_completed": False,
                "cleanup_failure_count": 1,
                "child_processes_terminated": self._children_terminated,
                "resources_released": False,
            }
        self._cleanup_called = True
        started = time.monotonic()
        failures = 0
        self.journal.append(marker="cleanup_started", status="started", status_code="phase_started")
        if not self.cancel():
            failures += 1
        failures += self._stop_background_processes()
        for action in reversed(tuple(actions)):
            try:
                if action() is False:
                    failures += 1
            except Exception:
                failures += 1
        extra = _count_extra_artifacts(self.run_root)
        if extra:
            failures += 1
        completed = failures == 0 and self._children_terminated
        self.journal.append(
            marker="cleanup_completed",
            status="completed" if completed else "failed",
            status_code="ok" if completed else "cleanup_incomplete",
            duration_ms=int((time.monotonic() - started) * 1000),
            counts={"cleanup_failures": failures, "extra_artifacts": extra},
        )
        receipt: dict[str, object] = {
            "schema": f"{SCHEMA_VERSION}.cleanup",
            "cleanup_attempted": True,
            "cleanup_completed": completed,
            "cleanup_failure_count": failures,
            "child_processes_terminated": self._children_terminated,
            "resources_released": completed,
        }
        _write_sanitized_json(self.run_root / "cleanup-receipt.json", receipt)
        return receipt


class SanitizedPhaseJournal:
    """Append-only status markers containing no operational secrets."""

    def __init__(self, run_root: Path, identity: CandidateIdentity) -> None:
        self.run_root = run_root
        self.identity = identity
        self.path = run_root / "phase-status.jsonl"
        self.records: list[dict[str, object]] = []
        try:
            run_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise HarnessFailure("journal_write_failed") from error

    def append(
        self,
        *,
        marker: str,
        status: Literal["started", "completed", "failed", "cancelled"],
        status_code: str,
        duration_ms: int | None = None,
        exit_code: int | None = None,
        counts: dict[str, int] | None = None,
    ) -> None:
        record: dict[str, object] = {
            "schema": SCHEMA_VERSION,
            "marker": marker,
            "status": status,
            "status_code": _safe_code(status_code) if status_code != "ok" else "ok",
            "observed_at": _utc_now(),
            "identity": self.identity.as_dict(),
        }
        if duration_ms is not None:
            record["duration_ms"] = max(0, int(duration_ms))
        if exit_code is not None:
            record["exit_code"] = int(exit_code)
        if counts is not None:
            record["counts"] = {
                key: max(0, int(value))
                for key, value in sorted(counts.items())
                if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
            }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise HarnessFailure("journal_write_failed") from error
        self.records.append(record)


def _live_plan_fail(code: str) -> NoReturn:
    raise LiveControlValidationError(code)


def _validate_plan_argv(value: Any, code: str = "live_control_plan_invalid") -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str)
        or not item
        or "\x00" in item
        or Path(item).is_absolute()
        or re.match(r"^(?:[A-Za-z]:|[\\/]{2})", item) is not None
        or ".." in Path(item).parts
        for item in value
    ):
        _live_plan_fail(code)
    # Return a fresh list so callers cannot mutate the source object while a
    # plan is executing.
    return list(value)


def _validate_plan_path(value: Any) -> str:
    """Validate a relative plan path before it is joined to an isolated root."""

    if not isinstance(value, str) or not value or "\x00" in value:
        _live_plan_fail("plan_path_invalid")
    candidate = Path(value)
    # ``Path.is_absolute`` covers native Windows/POSIX roots; the explicit
    # drive/UNC checks cover paths written for the other platform.
    if candidate.is_absolute() or re.match(r"^(?:[A-Za-z]:|[\\/]{2})", value):
        _live_plan_fail("plan_path_invalid")
    if any(part in {"..", "."} for part in candidate.parts):
        _live_plan_fail("plan_path_invalid")
    return value


def _is_reparse(path: Path) -> bool:
    try:
        stat_result = os.lstat(path)
        mode = stat_result.st_mode
        if stat.S_ISLNK(mode):
            return True
        attributes = getattr(stat_result, "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _resolve_isolated_path(
    value: Any,
    *,
    isolated_root: Path,
    must_exist: bool = False,
    allow_missing_parents: bool = False,
) -> str:
    relative = _validate_plan_path(value)
    try:
        root = isolated_root.resolve(strict=True)
    except OSError:
        _live_plan_fail("plan_path_outside_isolation")
    if _is_reparse(root):
        _live_plan_fail("plan_path_linked")
    target = root / relative
    try:
        resolved = target.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError):
        _live_plan_fail("plan_path_outside_isolation")
    current = root
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        exists = current.exists()
        if not exists:
            if (index != len(parts) - 1 and not allow_missing_parents) or must_exist:
                _live_plan_fail("plan_path_outside_isolation")
            continue
        if _is_reparse(current):
            _live_plan_fail("plan_path_linked")
        try:
            mode = os.lstat(current).st_mode
            if current.is_file() and getattr(os.stat(current), "st_nlink", 1) > 1:
                _live_plan_fail("plan_path_linked")
            if stat.S_ISREG(mode) and index != len(parts) - 1:
                _live_plan_fail("plan_path_outside_isolation")
        except OSError:
            _live_plan_fail("plan_path_outside_isolation")
    return str(resolved)


def _environment_value_is_local(
    key: str, value: str, *, working_directory: Path | None = None
) -> bool:
    """Reject host path injection in child environment overrides."""

    if key not in LOCAL_ENV_PATH_KEYS:
        return True
    if working_directory is None:
        return False
    root = working_directory.resolve(strict=False) if working_directory is not None else None
    for entry in value.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry)
        if candidate.is_absolute() or re.match(r"^(?:[A-Za-z]:|[\\/]{2})", entry):
            if root is None:
                return False
            try:
                candidate.resolve(strict=False).relative_to(root)
            except (OSError, ValueError, RuntimeError):
                return False
        elif ".." in candidate.parts:
            return False
    return True


def _validate_environment(
    value: Any, *, isolated_root: Path | None = None
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        _live_plan_fail("environment_invalid")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or key in SENSITIVE_ENV_KEYS
            or key not in LIVE_ENV_KEYS
            or not isinstance(item, str)
            or "\x00" in item
            or len(item) > 4096
            or not _environment_value_is_local(
                key, item, working_directory=isolated_root
            )
        ):
            _live_plan_fail("environment_invalid")
        normalized[key] = item
    return normalized


def _safe_environment(
    overrides: Mapping[str, str], *, working_directory: Path | None = None
) -> dict[str, str]:
    """Build a minimal child environment without host credentials/home state."""

    result: dict[str, str] = {}
    for key in ("SystemRoot", "ComSpec"):
        value = os.environ.get(key)
        if value:
            result[key] = value
    for key, value in overrides.items():
        if not _environment_value_is_local(
            key, value, working_directory=working_directory
        ):
            continue
        result[key] = value
    # Never copy sensitive values from arbitrary direct callers.  The public
    # runner constructor is also used by fixture tests, so this function must
    # enforce the credential/home boundary itself rather than relying on the
    # live-plan validator.
    for key in SENSITIVE_ENV_KEYS:
        result.pop(key, None)
    return result


def _validate_executable_spec(
    value: Any,
    *,
    isolated_root: Path | None,
    error_code: str,
    extra_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    expected = frozenset(
        {"argv", "executable_file", "executable_sha256", "argv_file_digests"}
    ) | extra_fields
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        _live_plan_fail(error_code)
    argv = _validate_plan_argv(value.get("argv"))
    executable_file = _validate_plan_path(value.get("executable_file"))
    digest = value.get("executable_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        _live_plan_fail(error_code)
    if argv[0] != executable_file:
        _live_plan_fail(error_code)
    raw_files = value.get("argv_file_digests")
    if not isinstance(raw_files, Mapping):
        _live_plan_fail(error_code)
    file_digests: dict[str, str] = {}
    for path, file_digest in raw_files.items():
        if not isinstance(path, str) or not isinstance(file_digest, str):
            _live_plan_fail(error_code)
        if re.fullmatch(r"[0-9a-fA-F]{64}", file_digest) is None:
            _live_plan_fail(error_code)
        if isolated_root is not None:
            resolved_file = _resolve_isolated_path(
                path, isolated_root=isolated_root, must_exist=True
            )
            if _file_sha256(resolved_file) != file_digest.lower():
                _live_plan_fail(error_code)
        file_digests[path] = file_digest.lower()
    if isolated_root is not None:
        resolved = _resolve_isolated_path(
            executable_file, isolated_root=isolated_root, must_exist=True
        )
        if _file_sha256(resolved) != digest.lower():
            _live_plan_fail(error_code)
    return {
        "argv": argv,
        "executable_file": executable_file,
        "executable_sha256": digest.lower(),
        "argv_file_digests": file_digests,
        **{
            key: value[key]
            for key in extra_fields
            if key in value
        },
    }


def _file_sha256(path: str) -> str:
    try:
        target = Path(path)
        if target.is_symlink() or not target.is_file() or os.stat(target).st_nlink != 1:
            raise OSError
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _validate_live_binding(
    identity: CandidateIdentity,
    binding: Mapping[str, Any],
    *,
    isolated_root: Path | None,
) -> dict[str, str]:
    expected_fields = frozenset(
        {"candidate_sha_file", "cli_package_file", "catalog_file", "route_file"}
    )
    if frozenset(binding) != expected_fields:
        _live_plan_fail("candidate_binding_mismatch")
    if isolated_root is None:
        paths = {key: _validate_plan_path(binding[key]) for key in expected_fields}
    else:
        paths = {
            key: _resolve_isolated_path(
                binding[key], isolated_root=isolated_root, must_exist=True
            )
            for key in expected_fields
        }
    try:
        candidate_text = Path(paths["candidate_sha_file"]).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        _live_plan_fail("candidate_sha_binding_mismatch")
    if candidate_text.lower() != identity.candidate_sha:
        _live_plan_fail("candidate_sha_binding_mismatch")
    checks = (
        ("cli_package_file", identity.cli_package_sha256, "cli_package_binding_mismatch"),
        ("catalog_file", identity.catalog_digest, "catalog_binding_mismatch"),
        ("route_file", identity.route_digest, "route_binding_mismatch"),
    )
    for field, expected, code in checks:
        if _file_sha256(paths[field]) != expected:
            _live_plan_fail(code)
    return paths


def _validate_sidecar_spec(value: Any, *, isolated_root: Path | None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != frozenset(
        {
            "argv",
            "executable_file",
            "executable_sha256",
            "argv_file_digests",
            "output_dir",
        }
    ):
        _live_plan_fail("helper_executable_binding_mismatch")
    executable = _validate_executable_spec(
        {
            key: value[key]
            for key in (
                "argv",
                "executable_file",
                "executable_sha256",
                "argv_file_digests",
            )
        },
        isolated_root=isolated_root,
        error_code="helper_executable_binding_mismatch",
    )
    output_dir = _validate_plan_path(value.get("output_dir"))
    if isolated_root is not None:
        output_dir = _resolve_isolated_path(
            output_dir, isolated_root=isolated_root, allow_missing_parents=True
        )
    return {**executable, "output_dir": output_dir}


def _validate_sidecars(
    value: Any, *, isolated_root: Path | None, run_root: Path | None
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping) or frozenset(value) != frozenset({"pre", "post"}):
        _live_plan_fail("live_control_plan_invalid")
    result: dict[str, list[dict[str, Any]]] = {}
    output_dirs: set[str] = set()
    for hop in ("pre", "post"):
        raw = value.get(hop)
        specs = raw if isinstance(raw, list) else [raw]
        if not specs or len(specs) > len(CONTROL_NAMES):
            _live_plan_fail("live_control_plan_invalid")
        normalized = [
            _validate_sidecar_spec(item, isolated_root=isolated_root) for item in specs
        ]
        for spec in normalized:
            directory_path = Path(spec["output_dir"])
            if run_root is not None:
                try:
                    directory_path.resolve().relative_to(run_root.resolve())
                except ValueError:
                    _live_plan_fail("plan_path_outside_isolation")
            directory = str(directory_path.resolve())
            if directory in output_dirs or any(
                Path(directory).is_relative_to(Path(existing))
                or Path(existing).is_relative_to(Path(directory))
                for existing in output_dirs
            ):
                _live_plan_fail("live_control_plan_invalid")
            output_dirs.add(directory)
        result[hop] = normalized
    return result


def load_live_control_plan(
    source: Path | str | Mapping[str, Any],
    *,
    isolated_root: Path | None = None,
    run_root: Path | None = None,
) -> dict[str, Any]:
    """Load and independently bind an explicitly authorized live plan.

    This function performs no subprocess or network work.  It is deliberately
    strict: a plan must carry all candidate/catalog/route digest bindings and
    exactly the eight semantic control labels before the live runner can start.
    """

    if isinstance(source, (str, Path)):
        source_path = Path(source)
        if (
            source_path.is_symlink()
            or not source_path.exists()
            or not source_path.is_file()
            or os.stat(source_path).st_nlink != 1
        ):
            _live_plan_fail("live_control_plan_missing")
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            _live_plan_fail("live_control_plan_invalid")
    else:
        payload = source
    if not isinstance(payload, Mapping) or frozenset(payload) != LIVE_PLAN_FIELDS:
        _live_plan_fail("live_control_plan_invalid")
    if payload.get("schema") != LIVE_PLAN_SCHEMA or payload.get("verification_scope") != LIVE_SCOPE:
        _live_plan_fail("live_control_plan_invalid")
    if payload.get("isolation_root") != ".":
        _live_plan_fail("plan_path_invalid")
    if isolated_root is not None:
        try:
            isolated_root = isolated_root.resolve(strict=True)
        except OSError:
            _live_plan_fail("plan_path_outside_isolation")

    raw_identity = payload.get("candidate_identity")
    if not isinstance(raw_identity, Mapping):
        _live_plan_fail("candidate_binding_mismatch")
    identity_fields = frozenset(
        {"candidate_sha", "cli_version", "cli_package_sha256", "catalog_digest", "route_digest"}
    )
    if frozenset(raw_identity) not in (identity_fields, identity_fields | {"cli_source_commit"}):
        _live_plan_fail("candidate_binding_mismatch")
    try:
        identity = CandidateIdentity(
            candidate_sha=raw_identity["candidate_sha"],
            cli_version=raw_identity["cli_version"],
            cli_package_sha256=raw_identity["cli_package_sha256"],
            catalog_digest=raw_identity["catalog_digest"],
            route_digest=raw_identity["route_digest"],
            cli_source_commit=raw_identity.get("cli_source_commit"),
        )
    except (KeyError, TypeError, ValueError):
        _live_plan_fail("candidate_binding_mismatch")

    binding = payload.get("binding")
    if not isinstance(binding, Mapping):
        _live_plan_fail("candidate_binding_mismatch")
    normalized_binding = _validate_live_binding(
        identity, binding, isolated_root=isolated_root
    )

    model = payload.get("catalog_model_entry_id")
    if (
        not isinstance(model, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model) is None
        or model != "gpt-5.6-sol"
    ):
        _live_plan_fail("catalog_binding_mismatch")

    cli = payload.get("cli")
    if not isinstance(cli, Mapping) or frozenset(cli) != frozenset(
        {
            "argv",
            "executable_file",
            "executable_sha256",
            "argv_file_digests",
            "cli_version",
        }
    ):
        _live_plan_fail("cli_executable_binding_mismatch")
    if cli.get("cli_version") != identity.cli_version:
        _live_plan_fail("cli_executable_binding_mismatch")
    normalized_cli = _validate_executable_spec(
        cli,
        isolated_root=isolated_root,
        error_code="cli_executable_binding_mismatch",
        extra_fields=frozenset({"cli_version"}),
    )
    normalized_cli["cli_version"] = cli["cli_version"]
    normalized_sidecars = _validate_sidecars(
        payload.get("sidecars"), isolated_root=isolated_root, run_root=run_root
    )
    normalized_environment = _validate_environment(
        payload.get("environment"), isolated_root=isolated_root
    )
    planner = payload.get("planner")
    if not isinstance(planner, Mapping) or frozenset(planner) != PLANNER_FIELDS:
        _live_plan_fail("planner_plan_incomplete")
    if planner.get("model_visible_plan") != "complete":
        _live_plan_fail("planner_plan_incomplete")
    if planner.get("hosted_only_disposition") not in LIVE_DISPOSITIONS or planner.get(
        "unknown_tag_disposition"
    ) not in LIVE_DISPOSITIONS:
        _live_plan_fail("planner_disposition_invalid")

    controls = payload.get("controls")
    if not isinstance(controls, list) or len(controls) != len(CONTROL_NAMES):
        _live_plan_fail("control_labels_incomplete")
    normalized_controls: list[dict[str, Any]] = []
    names: list[str] = []
    record_paths: dict[str, set[str]] = {"pre": set(), "post": set()}
    allowed_control_fields = frozenset({"name", "args", "capture", "pre_record", "post_record"})
    for item in controls:
        if not isinstance(item, Mapping) or not frozenset(item).issubset(allowed_control_fields):
            _live_plan_fail("live_control_plan_invalid")
        name = item.get("name")
        if not isinstance(name, str) or name not in CONTROL_NAME_SET:
            _live_plan_fail("control_labels_incomplete")
        names.append(name)
        raw_args = item.get("args", [])
        normalized: dict[str, Any] = {
            "name": name,
            "args": _validate_plan_argv(raw_args, "live_control_plan_invalid")
            if raw_args != []
            else [],
            "capture": item.get("capture", {}),
        }
        if not isinstance(normalized["capture"], Mapping):
            _live_plan_fail("live_control_plan_invalid")
        for field in ("pre_record", "post_record"):
            if field in item:
                if isolated_root is None:
                    normalized[field] = _validate_plan_path(item[field])
                else:
                    normalized[field] = _resolve_isolated_path(
                        item[field], isolated_root=isolated_root, allow_missing_parents=True
                    )
                normalized_path = str(Path(normalized[field]).resolve())
                if normalized_path in record_paths[field.removesuffix("_record")]:
                    _live_plan_fail("sidecar_capture_incomplete")
                record_paths[field.removesuffix("_record")].add(normalized_path)
        normalized_controls.append(normalized)
    if set(names) != CONTROL_NAME_SET or len(set(names)) != len(names):
        _live_plan_fail("control_labels_incomplete")

    replays = payload.get("replays")
    replay_names = frozenset({"identity", "mutation", "deletion", "loss"})
    if not isinstance(replays, Mapping) or frozenset(replays) != replay_names:
        _live_plan_fail("identity_replay_incomplete")
    normalized_replays: dict[str, dict[str, Any]] = {}
    for name in replay_names:
        spec = replays.get(name)
        if not isinstance(spec, Mapping) or frozenset(spec) != frozenset(
            {
                "argv",
                "executable_file",
                "executable_sha256",
                "argv_file_digests",
                "artifact_file",
                "case",
            }
        ) or spec.get("case") != name:
            _live_plan_fail("identity_replay_incomplete")
        replay = _validate_executable_spec(
            spec,
            isolated_root=isolated_root,
            error_code="helper_executable_binding_mismatch",
            extra_fields=frozenset({"artifact_file", "case"}),
        )
        artifact_file = _validate_plan_path(replay.get("artifact_file"))
        if isolated_root is not None:
            artifact_file = _resolve_isolated_path(
                artifact_file, isolated_root=isolated_root, allow_missing_parents=True
            )
            if run_root is not None:
                try:
                    Path(artifact_file).resolve().relative_to(run_root.resolve())
                except ValueError:
                    _live_plan_fail("plan_path_outside_isolation")
        replay["artifact_file"] = artifact_file
        normalized_replays[name] = replay

    return {
        "schema": LIVE_PLAN_SCHEMA,
        "verification_scope": LIVE_SCOPE,
        "isolation_root": ".",
        "candidate_identity": identity.as_dict(),
        "binding": normalized_binding,
        "catalog_model_entry_id": model,
        "cli": normalized_cli,
        "sidecars": normalized_sidecars,
        "controls": normalized_controls,
        "replays": normalized_replays,
        "environment": normalized_environment,
        "planner": dict(planner),
    }


def _load_manifest_builder() -> Any:
    # The evidence scripts are intentionally loosely coupled.  Importing the
    # builder lazily keeps fixture mode usable from a source checkout and does
    # not add any production routing dependency.
    try:
        from build_issue_62_control_manifest import (  # type: ignore[import-not-found]
            ManifestValidationError,
            build_manifest,
            reconcile_manifest,
            replay_manifest,
        )
    except ImportError as error:
        raise HarnessFailure("phase_exception") from error
    return ManifestValidationError, build_manifest, reconcile_manifest, replay_manifest


def _prepare_capture_dirs(sidecars: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    for specs in sidecars.values():
        for spec in specs:
            target = Path(str(spec["output_dir"]))
            try:
                if target.exists():
                    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                        raise OSError
                else:
                    target.mkdir(parents=True, exist_ok=False)
            except OSError:
                raise LiveControlValidationError("sidecar_capture_missing")


def _record_path_for_control(
    control: Mapping[str, Any],
    *,
    hop: str,
    specs: Sequence[Mapping[str, Any]],
    index: int,
) -> Path:
    def capture_files(root: Path) -> list[Path]:
        try:
            if root.is_symlink() or not root.is_dir():
                raise OSError
            entries = list(root.iterdir())
        except OSError:
            raise LiveControlValidationError("sidecar_capture_missing")
        if len(entries) > len(CONTROL_NAMES) or any(
            entry.is_symlink()
            or entry.is_dir()
            or entry.suffix.lower() != ".json"
            or os.stat(entry).st_nlink != 1
            for entry in entries
        ):
            raise LiveControlValidationError("sidecar_capture_incomplete")
        return sorted(entry for entry in entries if entry.is_file())

    explicit = control.get(f"{hop}_record")
    if explicit is not None:
        path = Path(str(explicit))
        for spec in specs:
            root = Path(str(spec["output_dir"]))
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            if path.resolve() not in {candidate.resolve() for candidate in capture_files(root)}:
                raise LiveControlValidationError("sidecar_capture_missing")
            return path
        raise LiveControlValidationError("sidecar_capture_missing")
    if len(specs) == len(CONTROL_NAMES):
        root = Path(str(specs[index]["output_dir"]))
        candidates = capture_files(root)
        if len(candidates) == 1:
            return candidates[0]
        raise LiveControlValidationError("sidecar_capture_missing")
    if len(specs) == 1:
        root = Path(str(specs[0]["output_dir"]))
        candidates = capture_files(root)
        if len(candidates) == len(CONTROL_NAMES):
            return candidates[index]
    raise LiveControlValidationError("sidecar_capture_missing")


def _read_sidecar_record(path: Path, *, hop: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or os.stat(path).st_nlink != 1:
            raise OSError
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LiveControlValidationError("sidecar_capture_incomplete")
    try:
        _ManifestValidationError, _build_manifest, _reconcile_manifest, _replay_manifest = (
            _load_manifest_builder()
        )
        from build_issue_62_control_manifest import sanitize_sidecar_record  # type: ignore[import-not-found]

        # Validate against the capture-only schema, but keep the raw
        # transport metadata in memory for ``build_manifest``.  The builder
        # drops capture IDs/hop fields before writing the canonical manifest.
        sanitize_sidecar_record(record, expected_hop=hop)
        return dict(record)
    except Exception as error:
        if isinstance(error, LiveControlValidationError):
            raise
        raise LiveControlValidationError("sidecar_capture_incomplete") from error


def _build_live_controls(
    plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    sidecars = plan["sidecars"]
    controls: list[dict[str, Any]] = []
    for index, control in enumerate(plan["controls"]):
        semantic = dict(control.get("capture", {}))
        semantic["name"] = control["name"]
        semantic["pre"] = _read_sidecar_record(
            _record_path_for_control(control, hop="pre", specs=sidecars["pre"], index=index),
            hop="pre",
        )
        semantic["post"] = _read_sidecar_record(
            _record_path_for_control(control, hop="post", specs=sidecars["post"], index=index),
            hop="post",
        )
        controls.append(semantic)
    return controls


def _manifest_candidate(plan: Mapping[str, Any]) -> dict[str, Any]:
    identity = plan["candidate_identity"]
    return {
        "codexhub_candidate_sha": identity["candidate_sha"],
        "cli_version": identity["cli_version"],
        "cli_source_commit": identity.get("cli_source_commit"),
        "cli_source_commit_status": (
            "published" if identity.get("cli_source_commit") else "not_published_by_registry"
        ),
        "cli_package_sha256": identity["cli_package_sha256"],
        "catalog_snapshot_sha256": identity["catalog_digest"],
        "catalog_model_entry_id": plan["catalog_model_entry_id"],
    }


def _runtime_argv(spec: Mapping[str, Any], *, isolated_root: Path) -> list[str]:
    executable = _resolve_isolated_path(
        spec["executable_file"], isolated_root=isolated_root, must_exist=True
    )
    for path, expected in spec.get("argv_file_digests", {}).items():
        resolved = _resolve_isolated_path(path, isolated_root=isolated_root, must_exist=True)
        if _file_sha256(resolved) != expected:
            raise LiveControlValidationError("helper_executable_binding_mismatch")
    argv = list(spec["argv"])
    argv[0] = executable
    return argv


def _remove_capture_dirs(
    sidecars: Mapping[str, Sequence[Mapping[str, Any]]], *, run_root: Path
) -> bool:
    failures = 0
    root = run_root.resolve()
    for specs in sidecars.values():
        for spec in specs:
            target = Path(str(spec["output_dir"])).resolve()
            try:
                target.relative_to(root)
                if target == root:
                    raise OSError
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    if not _remove_directory(target):
                        raise OSError
            except (OSError, ValueError):
                failures += 1
    return failures == 0


def _remove_replay_artifacts(
    replays: Mapping[str, Mapping[str, Any]], *, run_root: Path
) -> bool:
    failures = 0
    root = run_root.resolve()
    for replay in replays.values():
        target = Path(str(replay["artifact_file"])).resolve()
        try:
            target.relative_to(root)
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                target.unlink()
        except (OSError, ValueError):
            failures += 1
    return failures == 0


def _remove_directory(path: Path) -> bool:
    try:
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            if not path.is_dir():
                return False
            for index, _nested in enumerate(path.rglob("*"), start=1):
                if index >= MAX_NESTED_ARTIFACT_SCAN:
                    return False
            shutil.rmtree(path)
        return not path.exists()
    except OSError:
        return False


def _ensure_replay_artifacts_fresh(
    replays: Mapping[str, Mapping[str, Any]], *, run_root: Path
) -> None:
    """Reject a reused replay output before any child process starts."""

    root = run_root.resolve()
    seen: set[Path] = set()
    for replay in replays.values():
        target = Path(str(replay["artifact_file"])).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise LiveControlValidationError("plan_path_outside_isolation")
        if target in seen or target.exists() or target.is_symlink():
            raise LiveControlValidationError("stale_artifact")
        seen.add(target)


def _validate_replay_artifact(
    path: Path,
    *,
    case: str,
    candidate_sha: str,
    manifest_sha: str,
) -> str:
    # Diagnostic kept path-free; only existence is observed.
    # (The caller redacts all path values from journal artifacts.)
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        artifact_sha = _file_sha256(str(path))
        if re.fullmatch(r"[0-9a-f]{64}", artifact_sha) is None:
            raise OSError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LiveControlValidationError("identity_replay_artifact_missing")
    expected_fields = frozenset(
        {"schema", "case", "candidate_sha", "capture_manifest_sha256", "wire_replay", "outcome"}
    )
    if not isinstance(value, Mapping) or frozenset(value) != expected_fields:
        raise LiveControlValidationError("identity_replay_artifact_invalid")
    if (
        value.get("schema") != "codexhub.issue62.identity-replay.v1"
        or value.get("case") != case
        or value.get("candidate_sha") != candidate_sha
        or value.get("capture_manifest_sha256") != manifest_sha
        or value.get("wire_replay") is not True
        or value.get("outcome") != ("accepted" if case == "identity" else "rejected")
    ):
        raise LiveControlValidationError("identity_replay_artifact_invalid")
    return artifact_sha


def run_live_control(
    plan: Path | str | Mapping[str, Any],
    *,
    run_root: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    manifest_out: Path | None = None,
    isolated_root: Path | None = None,
    cancellation_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Execute an explicitly supplied live plan through bounded children.

    The result is intentionally non-qualifying even when every command,
    capture, and negative replay check succeeds.  An independent maintainer
    review is still required before any Issue #62 claim can be made.
    """

    try:
        if isolated_root is None:
            raise LiveControlValidationError("plan_path_outside_isolation")
        raw_isolation = Path(isolated_root)
        if _is_reparse(raw_isolation):
            raise LiveControlValidationError("plan_path_linked")
        isolation = raw_isolation.resolve(strict=True)
        if isinstance(plan, (str, Path)):
            raw_plan_path = Path(plan)
            if (
                raw_plan_path.is_symlink()
                or not raw_plan_path.is_file()
                or os.stat(raw_plan_path).st_nlink != 1
            ):
                raise LiveControlValidationError("plan_path_linked")
            plan_path = raw_plan_path.resolve(strict=True)
            plan_path.relative_to(isolation)
        run_path = Path(run_root)
        if not run_path.is_absolute():
            run_path = isolation / run_path
        if _is_reparse(run_path):
            raise LiveControlValidationError("plan_path_linked")
        run_path = run_path.resolve(strict=False)
        run_path.relative_to(isolation)
        if run_path == isolation or _is_reparse(run_path):
            raise LiveControlValidationError("plan_path_outside_isolation")
    except (OSError, ValueError, RuntimeError):
        raise LiveControlValidationError("plan_path_outside_isolation")
    try:
        loaded = load_live_control_plan(
            plan, isolated_root=isolation, run_root=run_path
        )
        identity = CandidateIdentity(**loaded["candidate_identity"])
    except LiveControlValidationError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise LiveControlValidationError("live_control_plan_invalid")
    output_manifest: Path | None = None
    if manifest_out is not None:
        try:
            output_manifest = Path(manifest_out)
            if not output_manifest.is_absolute():
                output_manifest = isolation / output_manifest
            if output_manifest.is_symlink():
                raise LiveControlValidationError("plan_path_linked")
            output_manifest = output_manifest.resolve(strict=False)
            output_manifest.relative_to(isolation)
            try:
                output_manifest.relative_to(run_path)
            except ValueError:
                pass
            else:
                raise LiveControlValidationError("plan_path_outside_isolation")
            if output_manifest.exists():
                raise LiveControlValidationError("stale_artifact")
        except LiveControlValidationError:
            raise
        except (OSError, ValueError, RuntimeError):
            raise LiveControlValidationError("plan_path_outside_isolation")
    case_temp = run_path / "temp"
    child_environment = dict(loaded["environment"])
    child_environment["TEMP"] = str(case_temp)
    child_environment["TMP"] = str(case_temp)
    try:
        runner = BoundedPhaseRunner(
            run_path,
            identity,
            timeout_seconds=timeout_seconds,
            environment=child_environment,
            working_directory=isolation,
        )
    except HarnessFailure:
        raise
    except (OSError, ValueError, RuntimeError):
        raise LiveControlValidationError("live_control_plan_invalid")
    handles: list[BackgroundProcess] = []
    result_status = "completed"
    result_code = "ok"
    manifest: dict[str, Any] | None = None
    manifest_reconciled = False
    replay_results: dict[str, str] = {}
    cleanup_actions: tuple[CleanupAction, ...] = (
        lambda: _remove_capture_dirs(loaded["sidecars"], run_root=run_path),
        lambda: _remove_replay_artifacts(loaded["replays"], run_root=run_path),
        lambda: _remove_directory(case_temp),
    )

    def is_cancelled() -> bool:
        return cancellation_event is not None and cancellation_event.is_set()

    try:
        runner.mark_phase("binding_validated")
        _ensure_replay_artifacts_fresh(loaded["replays"], run_root=run_path)
        if is_cancelled():
            result_status, result_code = "cancelled", "cancelled"
        else:
            try:
                case_temp.mkdir(parents=False, exist_ok=False)
            except OSError:
                result_status, result_code = "failed", "stale_artifact"
        if result_status == "completed":
            _prepare_capture_dirs(loaded["sidecars"])
            for hop in ("pre", "post"):
                for index, spec in enumerate(loaded["sidecars"][hop]):
                    try:
                        handles.append(
                            runner.start_background(
                                f"{hop}_sidecar_{index}",
                                _runtime_argv(spec, isolated_root=isolation),
                            )
                        )
                    except HarnessFailure as error:
                        result_status, result_code = "failed", error.code
                        break
                if result_status != "completed":
                    break

        if result_status == "completed" and any(
            handle.process.poll() is not None for handle in handles
        ):
            result_status, result_code = "failed", "sidecar_capture_incomplete"
        if result_status == "completed":
            for control in loaded["controls"]:
                if is_cancelled():
                    result_status, result_code = "cancelled", "cancelled"
                    break
                result = runner.run_subprocess(
                    f"control_{control['name']}",
                    [
                        *_runtime_argv(loaded["cli"], isolated_root=isolation),
                        *control["args"],
                    ],
                    cancellation_event=cancellation_event,
                )
                if result.status != "completed":
                    result_status, result_code = result.status, result.status_code
                    break

        # Stop sidecars before reading their atomic records.  Cleanup repeats
        # this operation defensively for cancellation/error paths.
        for handle in reversed(handles):
            if not runner.stop_background(handle) and result_status == "completed":
                result_status, result_code = "failed", "termination_failed"

        if result_status == "completed":
            try:
                controls = _build_live_controls(loaded)
                _ManifestValidationError, build_manifest, reconcile_manifest, replay_manifest = (
                    _load_manifest_builder()
                )
                manifest = build_manifest(
                    controls,
                    candidate_identity=_manifest_candidate(loaded),
                    verification_scope=LIVE_SCOPE,
                )
                report = reconcile_manifest(manifest)
                if not report["reconciled"]:
                    result_status, result_code = "failed", "manifest_reconcile_failed"
                else:
                    manifest_reconciled = True
                    if output_manifest is not None:
                        _write_sanitized_json(output_manifest, manifest)
            except LiveControlValidationError as error:
                result_status, result_code = "failed", error.code
            except Exception as error:
                result_status, result_code = "failed", "manifest_reconcile_failed"

        if result_status == "completed" and manifest is not None:
            for case in ("identity", "mutation", "deletion", "loss"):
                replay = replay_manifest(manifest, case)
                report = reconcile_manifest(replay)
                expected_reconciled = case == "identity"
                replay_results[case] = "pass" if report["reconciled"] is expected_reconciled else "fail"
                if replay_results[case] != "pass":
                    result_status, result_code = "failed", "identity_replay_incomplete"
                    break
            if result_status == "completed":
                for case in ("identity", "mutation", "deletion", "loss"):
                    if is_cancelled():
                        result_status, result_code = "cancelled", "cancelled"
                        break
                    replay_result = runner.run_subprocess(
                        f"replay_{case}",
                        _runtime_argv(loaded["replays"][case], isolated_root=isolation),
                        cancellation_event=cancellation_event,
                    )
                    if replay_result.status != "completed":
                        result_status, result_code = "failed", "identity_replay_incomplete"
                        break
                    replay = loaded["replays"][case]
                    artifact_sha = _validate_replay_artifact(
                        Path(replay["artifact_file"]),
                        case=case,
                        candidate_sha=identity.candidate_sha,
                        manifest_sha=str(manifest["capture_manifest_sha256"]),
                    )
                    replay_results[case] = f"artifact_bound:{artifact_sha}"
    except LiveControlValidationError as error:
        result_status, result_code = "failed", error.code
    except HarnessFailure as error:
        result_status, result_code = "failed", error.code
    except Exception:
        # Keep unexpected manifest/replay errors fail-closed and path-free.
        result_status, result_code = "failed", "phase_exception"
    finally:
        try:
            receipt = runner.cleanup(actions=cleanup_actions)
        except HarnessFailure:
            receipt = {
                "cleanup_attempted": True,
                "cleanup_completed": False,
                "cleanup_failure_count": 1,
                "child_processes_terminated": False,
                "resources_released": False,
            }
        if receipt.get("cleanup_completed") is not True and result_status == "completed":
            result_status, result_code = "failed", "cleanup_incomplete"

    output: dict[str, Any] = {
        "completed": result_status == "completed",
        "ready_for_issue62": False,
        "status": result_status,
        "status_code": result_code,
        "cleanup_completed": receipt.get("cleanup_completed") is True,
        "manifest_reconciled": manifest_reconciled,
        "replay": replay_results,
        "reason": "independent_review_required",
        "planner": loaded["planner"],
    }
    if manifest is not None:
        output["capture_manifest_sha256"] = manifest.get("capture_manifest_sha256")
    return output


def _fixture_identity(args: argparse.Namespace) -> CandidateIdentity:
    return CandidateIdentity(
        candidate_sha=args.candidate_sha,
        cli_version=args.cli_version,
        cli_package_sha256=args.cli_package_sha256,
        catalog_digest=args.catalog_digest,
        route_digest=args.route_digest,
        cli_source_commit=args.cli_source_commit,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--isolated-root", type=Path, help="Fresh-root parent for relative plan paths")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--fixture", choices=("success", "timeout", "nonzero", "cancelled", "cleanup-failure")
    )
    mode.add_argument(
        "--enable-live-control",
        action="store_true",
        help="Run an explicitly supplied authorized live-control plan.",
    )
    parser.add_argument("--plan", type=Path, help="Sanitized live-control plan JSON")
    parser.add_argument("--manifest-out", type=Path, help="Sanitized live manifest output")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--candidate-sha")
    parser.add_argument("--cli-version")
    parser.add_argument("--cli-package-sha256")
    parser.add_argument("--catalog-digest")
    parser.add_argument("--route-digest")
    parser.add_argument("--cli-source-commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.enable_live_control:
        if args.plan is None:
            result = {
                "completed": False,
                "ready_for_issue62": False,
                "status": "failed",
                "status_code": "live_control_plan_missing",
                "cleanup_completed": False,
                "reason": "live_control_plan_missing",
            }
            print(json.dumps(result, sort_keys=True))
            return 2
        try:
            result = run_live_control(
                args.plan,
                run_root=args.run_root,
                timeout_seconds=args.timeout_seconds,
                manifest_out=args.manifest_out,
                isolated_root=args.isolated_root,
            )
        except LiveControlValidationError as error:
            result = {
                "completed": False,
                "ready_for_issue62": False,
                "status": "failed",
                "status_code": error.code,
                "cleanup_completed": False,
                "reason": error.code,
            }
        except HarnessFailure as error:
            result = {
                "completed": False,
                "ready_for_issue62": False,
                "status": "failed",
                "status_code": error.code,
                "cleanup_completed": False,
                "reason": error.code,
            }
        except Exception:
            result = {
                "completed": False,
                "ready_for_issue62": False,
                "status": "failed",
                "status_code": "phase_exception",
                "cleanup_completed": False,
                "reason": "phase_exception",
            }
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("completed") is True and result.get("cleanup_completed") is True else 2

    fixture_fields = (
        "candidate_sha",
        "cli_version",
        "cli_package_sha256",
        "catalog_digest",
        "route_digest",
    )
    if any(getattr(args, field) is None for field in fixture_fields):
        raise SystemExit("fixture mode requires candidate identity arguments")
    identity = _fixture_identity(args)
    runner = BoundedPhaseRunner(
        args.run_root,
        identity,
        timeout_seconds=args.timeout_seconds,
    )
    cancellation_event = threading.Event()
    if args.fixture == "cancelled":
        cancellation_event.set()
    command = (sys.executable, "-c", "pass")
    if args.fixture == "timeout":
        command = (sys.executable, "-c", "import time; time.sleep(3600)")
    elif args.fixture == "nonzero":
        command = (sys.executable, "-c", "raise SystemExit(17)")
    if args.fixture == "cleanup-failure":
        result = runner.run_subprocess("catalog_sync", command)
        receipt = runner.cleanup(actions=(lambda: (_ for _ in ()).throw(RuntimeError("fixture")),))
    else:
        result = runner.run_subprocess(
            "catalog_sync", command, cancellation_event=cancellation_event
        )
        if result.status == "completed":
            runner.mark_phase("config_written")
            runner.mark_phase("sidecars_started")
            runner.mark_phase("gateway_started")
            result = runner.run_subprocess("cli", command, cancellation_event=cancellation_event)
        if result.status == "completed":
            runner.mark_phase("controls_started")
        receipt = runner.cleanup()
    print(
        json.dumps(
            {
                "completed": result.status == "completed" and receipt["cleanup_completed"] is True,
                "ready_for_issue62": False,
                "status": result.status,
                "status_code": result.status_code,
                "cleanup_completed": receipt["cleanup_completed"],
            },
            sort_keys=True,
        )
    )
    return 0 if result.status == "completed" and receipt["cleanup_completed"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
