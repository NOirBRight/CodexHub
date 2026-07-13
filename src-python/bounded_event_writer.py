"""Bounded, non-blocking JSONL event writing.

Callers hand this module already-sanitized mappings. ``enqueue`` serializes a
snapshot in memory and never opens, writes, flushes, rotates, or waits on a
sink. One background writer owns batching, sink failures, and JSONL recovery.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import errno
import json
from pathlib import Path
import threading
import time
from typing import Any, BinaryIO, Callable, Deque, Literal, Mapping, Protocol, Sequence
import weakref


WriterOutcome = Literal["drained", "timeout", "failed"]
WriterShutdownState = Literal["running", "stopping", "stopped"]


class EventSink(Protocol):
    """The writer-facing sink seam.

    Implementations receive complete JSONL records, each ending in exactly one
    newline. They may raise when storage is unavailable; those errors are
    contained by :class:`BoundedEventWriter`.
    """

    def append(self, records: Sequence[bytes]) -> None:
        """Append complete serialized records in their supplied order."""


@dataclass(frozen=True)
class RecoverySummary:
    """Bounded, content-free telemetry about recovered writer pressure."""

    overflow_records: int
    overflow_bytes: int
    failed_records: int
    failure_count: int
    failure_category_counts: tuple[tuple[str, int], ...]

    @property
    def failure_categories(self) -> tuple[str, ...]:
        return tuple(category for category, _count in self.failure_category_counts)


RecoveryRecordFactory = Callable[[RecoverySummary], Mapping[str, Any]]
RotationOperation = Callable[[], None]


@dataclass(frozen=True)
class BoundedEventWriterStatus:
    """Sanitized observable state for a writer.

    Counts describe accepted telemetry records only. Recovery records generated
    by the writer are exposed separately and never reveal record content or a
    raw sink path.
    """

    queued_records: int
    queued_bytes: int
    accepted_records: int
    written_records: int
    dropped_records: int
    dropped_bytes: int
    failure_count: int
    last_failure_category: str | None
    last_failure_time: float | None
    recovery_events_written: int
    recovery_pending: bool
    shutdown_state: WriterShutdownState
    writer_alive: bool
    writer_generation: int


@dataclass(frozen=True)
class BoundedEventWriterResult:
    """Result of a bounded flush or shutdown operation."""

    outcome: WriterOutcome
    status: BoundedEventWriterStatus

    @property
    def completed(self) -> bool:
        return self.outcome == "drained"


@dataclass(frozen=True)
class _QueuedRecord:
    sequence: int
    data: bytes

    @property
    def byte_count(self) -> int:
        return len(self.data)


@dataclass
class _PendingRecovery:
    overflow_records: int = 0
    overflow_bytes: int = 0
    failed_records: int = 0
    failure_count: int = 0
    failure_categories: dict[str, int] = field(default_factory=dict)

    def has_data(self) -> bool:
        return any(
            (
                self.overflow_records,
                self.overflow_bytes,
                self.failed_records,
                self.failure_count,
            )
        )

    def snapshot(self) -> RecoverySummary:
        return RecoverySummary(
            overflow_records=self.overflow_records,
            overflow_bytes=self.overflow_bytes,
            failed_records=self.failed_records,
            failure_count=self.failure_count,
            failure_category_counts=tuple(sorted(self.failure_categories.items())),
        )

    def subtract(self, summary: RecoverySummary) -> None:
        self.overflow_records = max(0, self.overflow_records - summary.overflow_records)
        self.overflow_bytes = max(0, self.overflow_bytes - summary.overflow_bytes)
        self.failed_records = max(0, self.failed_records - summary.failed_records)
        self.failure_count = max(0, self.failure_count - summary.failure_count)
        for category, count in summary.failure_category_counts:
            remaining = self.failure_categories.get(category, 0) - count
            if remaining > 0:
                self.failure_categories[category] = remaining
            else:
                self.failure_categories.pop(category, None)

    def add_failure(self, category: str, record_count: int) -> None:
        self.failed_records += record_count
        self.failure_count += 1
        # Failure categories are fixed, short labels chosen by this module.
        # Keep the aggregation itself bounded even if a future category changes.
        if category in self.failure_categories or len(self.failure_categories) < 8:
            self.failure_categories[category] = self.failure_categories.get(category, 0) + 1


class JsonlFileSink:
    """Append complete JSONL records while repairing a previous partial tail.

    The sink deliberately opens a file only from the writer thread. If storage
    fails midway through a record, the next append discards that unterminated
    tail before accepting more records, so a partial serialization is never
    exposed as a valid JSONL line.
    """

    _TAIL_SCAN_BYTES = 64 * 1024

    def __init__(
        self,
        path: Path,
        *,
        open_file: Callable[[Path, str], BinaryIO] | None = None,
    ) -> None:
        self._path = path
        self._identity_path = path.expanduser().resolve()
        self._open_file = open_file or self._default_open_file
        self._lock = threading.Lock()

    def __hash__(self) -> int:
        return hash(self._identity_path)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, JsonlFileSink) and self._identity_path == other._identity_path

    @staticmethod
    def _default_open_file(path: Path, mode: str) -> BinaryIO:
        return path.open(mode)

    def append(self, records: Sequence[bytes]) -> None:
        if not records:
            return
        if any(not record.endswith(b"\n") or b"\n" in record[:-1] for record in records):
            raise ValueError("event sink requires complete one-line JSONL records")

        payload = b"".join(records)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._open_file(self._path, "a+b") as handle:
                self._repair_partial_tail(handle)
                self._write_all(handle, payload)
                handle.flush()

    @classmethod
    def _repair_partial_tail(cls, handle: BinaryIO) -> None:
        handle.seek(0, 2)
        end = handle.tell()
        if end == 0:
            return

        handle.seek(end - 1)
        if handle.read(1) == b"\n":
            return

        cursor = end
        while cursor:
            start = max(0, cursor - cls._TAIL_SCAN_BYTES)
            handle.seek(start)
            chunk = handle.read(cursor - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                handle.seek(start + newline + 1)
                handle.truncate()
                handle.flush()
                return
            cursor = start

        handle.seek(0)
        handle.truncate()
        handle.flush()

    @staticmethod
    def _write_all(handle: BinaryIO, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            written = handle.write(payload[offset:])
            if not isinstance(written, int) or written <= 0:
                raise OSError("event sink made no write progress")
            offset += written


class BoundedEventWriter:
    """Own a bounded event queue and exactly one background writer lifecycle.

    ``enqueue`` is safe for concurrent request threads: it performs only
    in-memory serialization and lock-protected queue bookkeeping. The sink,
    batching, partial-write cleanup, failure isolation, and aggregate recovery
    telemetry all stay behind this module's interface.
    """

    _sink_owners_lock = threading.Lock()
    _weak_sink_owners: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
    _strong_sink_owners: dict[int, Any] = {}

    def __init__(
        self,
        sink: EventSink,
        *,
        max_records: int,
        max_bytes: int,
        batch_max_records: int = 128,
        batch_max_bytes: int = 256 * 1024,
        recovery_record_factory: RecoveryRecordFactory | None = None,
        clock: Callable[[], float] = time.time,
        thread_name: str = "bounded-event-writer",
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if batch_max_records < 1:
            raise ValueError("batch_max_records must be positive")
        if batch_max_bytes < 1:
            raise ValueError("batch_max_bytes must be positive")

        self._sink = sink
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._batch_max_records = batch_max_records
        self._batch_max_bytes = batch_max_bytes
        self._recovery_record_factory = recovery_record_factory or _default_recovery_record
        self._clock = clock
        self._thread_name = thread_name

        self._condition = threading.Condition(threading.RLock())
        self._queue: Deque[_QueuedRecord] = deque()
        # Pending counts include a batch currently being written. This keeps the
        # configured bounds strict even while storage is slow.
        self._pending_records = 0
        self._pending_bytes = 0
        self._next_sequence = 1
        self._completed_sequence = 0

        self._accepted_records = 0
        self._written_records = 0
        self._dropped_records = 0
        self._dropped_bytes = 0
        self._failure_count = 0
        self._last_failure_category: str | None = None
        self._last_failure_time: float | None = None
        self._recovery_events_written = 0
        self._pending_recovery = _PendingRecovery()
        self._recovery_inflight = False
        self._unresolved_failure = False

        self._shutdown_state: WriterShutdownState = "running"
        self._worker: threading.Thread | None = None
        self._writer_generation = 0
        self._rotation_target_sequence: int | None = None
        self._sink_ownership: Literal["weak", "strong"]
        self._claim_sink_ownership()

    def enqueue(self, record: Mapping[str, Any]) -> bool:
        """Attempt a non-blocking in-memory enqueue of one sanitized record."""

        try:
            data = _serialize_record(record)
        except Exception:
            with self._condition:
                self._record_failure_locked("serialization_rejected", record_count=1, byte_count=0)
                self._condition.notify_all()
            return False

        byte_count = len(data)
        with self._condition:
            if self._shutdown_state != "running":
                self._dropped_records += 1
                self._dropped_bytes += byte_count
                self._condition.notify_all()
                return False
            if self._pending_records >= self._max_records or self._pending_bytes + byte_count > self._max_bytes:
                self._dropped_records += 1
                self._dropped_bytes += byte_count
                self._pending_recovery.overflow_records += 1
                self._pending_recovery.overflow_bytes += byte_count
                self._condition.notify_all()
                return False

            queued = _QueuedRecord(sequence=self._next_sequence, data=data)
            self._next_sequence += 1
            self._queue.append(queued)
            self._pending_records += 1
            self._pending_bytes += byte_count
            self._accepted_records += 1
            self._ensure_worker_locked()
            self._condition.notify_all()
            return True

    def status(self) -> BoundedEventWriterStatus:
        """Return only sanitized counters and lifecycle state."""

        with self._condition:
            return self._status_locked()

    def flush(self, timeout: float = 5.0) -> BoundedEventWriterResult:
        """Boundedly process records accepted before this call."""

        with self._condition:
            target_sequence = self._next_sequence - 1
            self._ensure_worker_locked()
            self._condition.notify_all()
        return self._wait_for(target_sequence, timeout)

    def rotate(self, operation: RotationOperation, timeout: float = 5.0) -> BoundedEventWriterResult:
        """Fence sink writes around a caller-owned, bounded rotation operation.

        The caller owns the rotation policy and its file changes. This module
        drains records accepted before the fence, holds later records in memory
        without blocking their enqueuers, then releases them only after the
        supplied operation returns.
        """

        with self._condition:
            if self._shutdown_state != "running" or self._rotation_target_sequence is not None:
                return BoundedEventWriterResult("failed", self._status_locked())
            target_sequence = self._next_sequence - 1
            self._rotation_target_sequence = target_sequence
            self._ensure_worker_locked()
            self._condition.notify_all()

        fenced = self._wait_for(target_sequence, timeout)
        if fenced.outcome == "timeout":
            with self._condition:
                self._rotation_target_sequence = None
                self._condition.notify_all()
            return fenced

        try:
            operation()
        except BaseException:
            with self._condition:
                self._record_failure_locked("rotation_failed", record_count=0, byte_count=0)
                self._rotation_target_sequence = None
                self._condition.notify_all()
                return self._result_locked()

        with self._condition:
            self._rotation_target_sequence = None
            self._condition.notify_all()
            return self._result_locked()

    def shutdown(self, timeout: float = 5.0) -> BoundedEventWriterResult:
        """Reject later enqueues and boundedly drain records accepted so far."""

        with self._condition:
            if self._shutdown_state == "running":
                self._shutdown_state = "stopping"
            target_sequence = self._next_sequence - 1
            self._ensure_worker_locked()
            self._condition.notify_all()

        result = self._wait_for(target_sequence, timeout)
        if result.outcome == "timeout":
            return result

        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            worker = self._worker
            self._condition.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0.0, deadline - time.monotonic()))

        release_sink_ownership = False
        with self._condition:
            if worker is not None and worker.is_alive():
                return BoundedEventWriterResult("timeout", self._status_locked())
            if self._pending_records == 0:
                self._shutdown_state = "stopped"
                release_sink_ownership = True
            result = self._result_locked()
        if release_sink_ownership:
            self._release_sink_ownership()
        return result

    def _wait_for(self, target_sequence: int, timeout: float) -> BoundedEventWriterResult:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while self._completed_sequence < target_sequence or self._recovery_inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return BoundedEventWriterResult("timeout", self._status_locked())
                self._condition.wait(remaining)
            return self._result_locked()

    def _result_locked(self) -> BoundedEventWriterResult:
        outcome: WriterOutcome = "failed" if self._unresolved_failure else "drained"
        return BoundedEventWriterResult(outcome, self._status_locked())

    def _ensure_worker_locked(self) -> None:
        if self._shutdown_state not in {"running", "stopping"}:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        if not self._queue:
            return
        try:
            worker = threading.Thread(target=self._writer_loop, name=self._thread_name, daemon=True)
            self._worker = worker
            self._writer_generation += 1
            worker.start()
        except Exception:
            self._worker = None
            self._record_failure_locked("writer_start_failed", record_count=0, byte_count=0)

    def _claim_sink_ownership(self) -> None:
        with self._sink_owners_lock:
            try:
                existing = self._weak_sink_owners.get(self._sink)
            except TypeError:
                existing = self._strong_sink_owners.get(id(self._sink))
                ownership = "strong"
            else:
                ownership = "weak"
            if existing is not None and existing._shutdown_state != "stopped":
                raise ValueError("an active BoundedEventWriter already owns this sink")
            if ownership == "weak":
                self._weak_sink_owners[self._sink] = self
            else:
                self._strong_sink_owners[id(self._sink)] = self
            self._sink_ownership = ownership

    def _release_sink_ownership(self) -> None:
        with self._sink_owners_lock:
            if self._sink_ownership == "weak":
                try:
                    if self._weak_sink_owners.get(self._sink) is self:
                        del self._weak_sink_owners[self._sink]
                except TypeError:
                    return
            elif self._strong_sink_owners.get(id(self._sink)) is self:
                self._strong_sink_owners.pop(id(self._sink), None)

    def _writer_loop(self) -> None:
        current_worker = threading.current_thread()
        inflight: Sequence[_QueuedRecord] = ()
        try:
            while True:
                with self._condition:
                    while not self._queue:
                        if self._shutdown_state == "stopping":
                            return
                        self._condition.wait()
                    while (
                        self._rotation_target_sequence is not None
                        and self._queue[0].sequence > self._rotation_target_sequence
                    ):
                        self._condition.wait()
                        if not self._queue:
                            break
                    if not self._queue:
                        continue
                    inflight = self._take_batch_locked()

                try:
                    self._sink.append([record.data for record in inflight])
                except Exception as exc:
                    with self._condition:
                        self._complete_batch_locked(inflight)
                        self._record_failure_locked(
                            _failure_category(exc),
                            record_count=len(inflight),
                            byte_count=sum(record.byte_count for record in inflight),
                        )
                        self._condition.notify_all()
                    inflight = ()
                    continue

                with self._condition:
                    self._complete_batch_locked(inflight)
                    self._written_records += len(inflight)
                    recovery = self._pending_recovery.snapshot() if self._pending_recovery.has_data() else None
                    self._recovery_inflight = recovery is not None
                    self._condition.notify_all()
                inflight = ()

                if recovery is not None:
                    self._emit_recovery(recovery)
        except BaseException:
            with self._condition:
                if inflight:
                    self._complete_batch_locked(inflight)
                    self._record_failure_locked(
                        "writer_crash",
                        record_count=len(inflight),
                        byte_count=sum(record.byte_count for record in inflight),
                    )
                else:
                    self._record_failure_locked("writer_crash", record_count=0, byte_count=0)
                self._condition.notify_all()
        finally:
            release_sink_ownership = False
            with self._condition:
                if self._worker is current_worker:
                    self._worker = None
                if self._shutdown_state == "stopping" and self._pending_records == 0:
                    self._shutdown_state = "stopped"
                    release_sink_ownership = True
                self._condition.notify_all()
            if release_sink_ownership:
                self._release_sink_ownership()

    def _take_batch_locked(self) -> list[_QueuedRecord]:
        batch: list[_QueuedRecord] = []
        batch_bytes = 0
        while self._queue and len(batch) < self._batch_max_records:
            next_record = self._queue[0]
            if (
                self._rotation_target_sequence is not None
                and next_record.sequence > self._rotation_target_sequence
            ):
                break
            if batch and batch_bytes + next_record.byte_count > self._batch_max_bytes:
                break
            batch.append(self._queue.popleft())
            batch_bytes += next_record.byte_count
        return batch

    def _complete_batch_locked(self, batch: Sequence[_QueuedRecord]) -> None:
        if not batch:
            return
        self._pending_records -= len(batch)
        self._pending_bytes -= sum(record.byte_count for record in batch)
        self._completed_sequence = max(self._completed_sequence, batch[-1].sequence)

    def _emit_recovery(self, summary: RecoverySummary) -> None:
        try:
            recovery_record = self._recovery_record_factory(summary)
            self._sink.append([_serialize_record(recovery_record)])
        except BaseException:
            with self._condition:
                # Do not feed this failure back into the same aggregate. The
                # original summary remains pending and can be retried after a
                # future normal write succeeds.
                self._record_failure_locked(
                    "recovery_emit_failed",
                    record_count=0,
                    byte_count=0,
                    include_recovery=False,
                )
                self._recovery_inflight = False
                self._condition.notify_all()
            return

        with self._condition:
            self._pending_recovery.subtract(summary)
            self._recovery_events_written += 1
            if not self._pending_recovery.has_data():
                self._unresolved_failure = False
            self._recovery_inflight = False
            self._condition.notify_all()

    def _record_failure_locked(
        self,
        category: str,
        *,
        record_count: int,
        byte_count: int,
        include_recovery: bool = True,
    ) -> None:
        self._dropped_records += record_count
        self._dropped_bytes += byte_count
        self._failure_count += 1
        self._last_failure_category = category
        self._last_failure_time = self._clock()
        self._unresolved_failure = True
        if include_recovery:
            self._pending_recovery.add_failure(category, record_count)

    def _status_locked(self) -> BoundedEventWriterStatus:
        return BoundedEventWriterStatus(
            queued_records=self._pending_records,
            queued_bytes=self._pending_bytes,
            accepted_records=self._accepted_records,
            written_records=self._written_records,
            dropped_records=self._dropped_records,
            dropped_bytes=self._dropped_bytes,
            failure_count=self._failure_count,
            last_failure_category=self._last_failure_category,
            last_failure_time=self._last_failure_time,
            recovery_events_written=self._recovery_events_written,
            recovery_pending=self._pending_recovery.has_data() or self._recovery_inflight,
            shutdown_state=self._shutdown_state,
            writer_alive=self._worker is not None and self._worker.is_alive(),
            writer_generation=self._writer_generation,
        )


def _serialize_record(record: Mapping[str, Any]) -> bytes:
    if not isinstance(record, Mapping):
        raise TypeError("event record must be a mapping")
    text = json.dumps(dict(record), ensure_ascii=True, separators=(",", ":"))
    return text.encode("utf-8") + b"\n"


def _default_recovery_record(summary: RecoverySummary) -> Mapping[str, Any]:
    return {
        "event": "telemetry_writer_recovered",
        "overflow_records": summary.overflow_records,
        "overflow_bytes": summary.overflow_bytes,
        "failed_records": summary.failed_records,
        "failure_count": summary.failure_count,
        "failure_categories": list(summary.failure_categories),
    }


def _failure_category(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return "disk_full"
    return "sink_error"
