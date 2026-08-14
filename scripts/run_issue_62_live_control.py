"""Run the bounded, sanitized orchestration envelope for Issue #62.

This module deliberately separates orchestration from the real upstream
controls.  The default command-line mode is ``--fixture`` and therefore cannot
qualify Issue #62.  A future live runner can reuse :class:`BoundedPhaseRunner`
with real command/phase callbacks after the exact candidate and route have been
reviewed.

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

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
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
from typing import Callable, Literal, Sequence


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
    }
)


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
        if not argv or any(not isinstance(item, str) or not item for item in argv):
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
    parser.add_argument(
        "--fixture", choices=("success", "timeout", "nonzero", "cancelled", "cleanup-failure"), required=True
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--cli-version", required=True)
    parser.add_argument("--cli-package-sha256", required=True)
    parser.add_argument("--catalog-digest", required=True)
    parser.add_argument("--route-digest", required=True)
    parser.add_argument("--cli-source-commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
