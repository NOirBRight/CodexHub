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
        "candidate_sha_binding_mismatch",
        "cli_package_binding_mismatch",
        "catalog_binding_mismatch",
        "route_binding_mismatch",
        "control_labels_incomplete",
        "sidecar_capture_missing",
        "sidecar_capture_incomplete",
        "identity_replay_incomplete",
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
        "candidate_identity",
        "binding",
        "catalog_model_entry_id",
        "cli",
        "sidecars",
        "controls",
        "replays",
    }
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
            return True
        if os.name == "nt":
            # ``terminate`` only signals the direct child on Windows.  The
            # Gateway/CLI can spawn grandchildren, so use taskkill's tree mode
            # and suppress all command output.
            subprocess.run(
                ["taskkill", "/PID", str(int(process.pid)), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
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
    ) -> None:
        _ensure_fresh_run_root(run_root)
        self.run_root = run_root
        self.identity = identity
        self.timeout_seconds = _validate_timeout(timeout_seconds)
        self._process_factory = process_factory or subprocess.Popen
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
        not isinstance(item, str) or not item or "\x00" in item for item in value
    ):
        _live_plan_fail(code)
    # Return a fresh list so callers cannot mutate the source object while a
    # plan is executing.
    return list(value)


def _validate_plan_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _live_plan_fail("live_control_plan_invalid")
    return value


def _file_sha256(path: str) -> str:
    try:
        target = Path(path)
        if target.is_symlink() or not target.is_file():
            raise OSError
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _validate_live_binding(
    identity: CandidateIdentity, binding: Mapping[str, Any]
) -> dict[str, str]:
    expected_fields = frozenset(
        {"candidate_sha_file", "cli_package_file", "catalog_file", "route_file"}
    )
    if frozenset(binding) != expected_fields:
        _live_plan_fail("candidate_binding_mismatch")
    paths = {key: _validate_plan_path(binding[key]) for key in expected_fields}
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


def _validate_sidecar_spec(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != frozenset({"argv", "output_dir"}):
        _live_plan_fail("live_control_plan_invalid")
    return {
        "argv": _validate_plan_argv(value.get("argv")),
        "output_dir": _validate_plan_path(value.get("output_dir")),
    }


def _validate_sidecars(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping) or frozenset(value) != frozenset({"pre", "post"}):
        _live_plan_fail("live_control_plan_invalid")
    result: dict[str, list[dict[str, Any]]] = {}
    output_dirs: set[str] = set()
    for hop in ("pre", "post"):
        raw = value.get(hop)
        specs = raw if isinstance(raw, list) else [raw]
        if not specs or len(specs) > len(CONTROL_NAMES):
            _live_plan_fail("live_control_plan_invalid")
        normalized = [_validate_sidecar_spec(item) for item in specs]
        for spec in normalized:
            directory = str(Path(spec["output_dir"]).resolve())
            if directory in output_dirs:
                _live_plan_fail("live_control_plan_invalid")
            output_dirs.add(directory)
        result[hop] = normalized
    return result


def load_live_control_plan(source: Path | str | Mapping[str, Any]) -> dict[str, Any]:
    """Load and independently bind an explicitly authorized live plan.

    This function performs no subprocess or network work.  It is deliberately
    strict: a plan must carry all candidate/catalog/route digest bindings and
    exactly the eight semantic control labels before the live runner can start.
    """

    if isinstance(source, (str, Path)):
        source_path = Path(source)
        if not source_path.exists() or not source_path.is_file():
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
    normalized_binding = _validate_live_binding(identity, binding)

    model = payload.get("catalog_model_entry_id")
    if (
        not isinstance(model, str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", model) is None
        or model != "gpt-5.6-sol"
    ):
        _live_plan_fail("catalog_binding_mismatch")

    cli = payload.get("cli")
    if not isinstance(cli, Mapping) or frozenset(cli) != frozenset({"argv"}):
        _live_plan_fail("live_control_plan_invalid")
    normalized_cli = {"argv": _validate_plan_argv(cli.get("argv"))}
    normalized_sidecars = _validate_sidecars(payload.get("sidecars"))

    controls = payload.get("controls")
    if not isinstance(controls, list) or len(controls) != len(CONTROL_NAMES):
        _live_plan_fail("control_labels_incomplete")
    normalized_controls: list[dict[str, Any]] = []
    names: list[str] = []
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
                normalized[field] = _validate_plan_path(item[field])
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
        if not isinstance(spec, Mapping) or frozenset(spec) != frozenset({"argv"}):
            _live_plan_fail("identity_replay_incomplete")
        normalized_replays[name] = {"argv": _validate_plan_argv(spec.get("argv"))}

    return {
        "schema": LIVE_PLAN_SCHEMA,
        "verification_scope": LIVE_SCOPE,
        "candidate_identity": identity.as_dict(),
        "binding": normalized_binding,
        "catalog_model_entry_id": model,
        "cli": normalized_cli,
        "sidecars": normalized_sidecars,
        "controls": normalized_controls,
        "replays": normalized_replays,
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
    explicit = control.get(f"{hop}_record")
    if explicit is not None:
        path = Path(str(explicit))
        for spec in specs:
            root = Path(str(spec["output_dir"]))
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            return path
        raise LiveControlValidationError("sidecar_capture_missing")
    if len(specs) == len(CONTROL_NAMES):
        root = Path(str(specs[index]["output_dir"]))
        candidates = sorted(path for path in root.glob("*.json") if path.is_file())
        if len(candidates) == 1:
            return candidates[0]
        raise LiveControlValidationError("sidecar_capture_missing")
    if len(specs) == 1:
        root = Path(str(specs[0]["output_dir"]))
        candidates = sorted(path for path in root.glob("*.json") if path.is_file())
        if len(candidates) == len(CONTROL_NAMES):
            return candidates[index]
    raise LiveControlValidationError("sidecar_capture_missing")


def _read_sidecar_record(path: Path, *, hop: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
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


def run_live_control(
    plan: Path | str | Mapping[str, Any],
    *,
    run_root: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    manifest_out: Path | None = None,
) -> dict[str, Any]:
    """Execute an explicitly supplied live plan through bounded children.

    The result is intentionally non-qualifying even when every command,
    capture, and negative replay check succeeds.  An independent maintainer
    review is still required before any Issue #62 claim can be made.
    """

    loaded = load_live_control_plan(plan)
    identity = CandidateIdentity(**loaded["candidate_identity"])
    runner = BoundedPhaseRunner(run_root, identity, timeout_seconds=timeout_seconds)
    handles: list[BackgroundProcess] = []
    result_status = "completed"
    result_code = "ok"
    manifest: dict[str, Any] | None = None
    manifest_reconciled = False
    replay_results: dict[str, str] = {}
    try:
        runner.mark_phase("binding_validated")
        _prepare_capture_dirs(loaded["sidecars"])
        for hop in ("pre", "post"):
            for index, spec in enumerate(loaded["sidecars"][hop]):
                try:
                    handles.append(runner.start_background(f"{hop}_sidecar_{index}", spec["argv"]))
                except HarnessFailure as error:
                    result_status, result_code = "failed", error.code
                    break
            if result_status != "completed":
                break

        if result_status == "completed":
            for control in loaded["controls"]:
                result = runner.run_subprocess(
                    f"control_{control['name']}",
                    [*loaded["cli"]["argv"], *control["args"]],
                )
                if result.status != "completed":
                    result_status, result_code = result.status, result.status_code
                    break

        # Stop sidecars before reading their atomic records.  Cleanup repeats
        # this operation defensively for cancellation/error paths.
        for handle in reversed(handles):
            if not runner.stop_background(handle):
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
                    if manifest_out is not None:
                        _write_sanitized_json(manifest_out, manifest)
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
                    replay_result = runner.run_subprocess(
                        f"replay_{case}", loaded["replays"][case]["argv"]
                    )
                    if replay_result.status != "completed":
                        result_status, result_code = "failed", "identity_replay_incomplete"
                        break
    except LiveControlValidationError as error:
        result_status, result_code = "failed", error.code
    except HarnessFailure as error:
        result_status, result_code = "failed", error.code
    finally:
        try:
            receipt = runner.cleanup()
        except HarnessFailure:
            receipt = {
                "cleanup_attempted": True,
                "cleanup_completed": False,
                "cleanup_failure_count": 1,
                "child_processes_terminated": False,
                "resources_released": False,
            }
        if receipt.get("cleanup_completed") is not True:
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
