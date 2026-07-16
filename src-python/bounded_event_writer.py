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
import math
from pathlib import Path
import threading
import time
from typing import Any, BinaryIO, Callable, Deque, Literal, Mapping, Protocol, Sequence
import weakref


WriterOutcome = Literal["drained", "timeout", "failed"]
WriterShutdownState = Literal["running", "stopping", "stopped"]


class _RecordTooLarge(ValueError):
    """Raised when bounded admission proves a record cannot fit in the queue."""


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
        # A crash with retained backlog gets exactly one automatic replacement.
        # A successful write resets this guard for a later, independent crash.
        self._automatic_restart_used = False
        self._rotation_target_sequence: int | None = None
        self._sink_ownership: Literal["weak", "strong"]
        self._claim_sink_ownership()

    def enqueue(self, record: Mapping[str, Any]) -> bool:
        """Attempt a non-blocking in-memory enqueue of one sanitized record."""

        with self._condition:
            if self._shutdown_state != "running":
                self._dropped_records += 1
                self._condition.notify_all()
                return False
            if self._pending_records >= self._max_records:
                self._record_overflow_locked(byte_count=0)
                self._condition.notify_all()
                return False
            admission_limit = self._max_bytes

        try:
            data = _serialize_record(record, max_bytes=admission_limit)
        except _RecordTooLarge:
            with self._condition:
                if self._shutdown_state == "running":
                    self._record_overflow_locked(byte_count=0)
                else:
                    self._dropped_records += 1
                self._condition.notify_all()
            return False
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
                self._record_overflow_locked(byte_count=byte_count)
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
        writer_crashed = False
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
                    self._automatic_restart_used = False
                    recovery = self._pending_recovery.snapshot() if self._pending_recovery.has_data() else None
                    self._recovery_inflight = recovery is not None
                    self._condition.notify_all()
                inflight = ()

                if recovery is not None:
                    self._emit_recovery(recovery)
        except BaseException:
            writer_crashed = True
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
                if writer_crashed and self._queue and not self._automatic_restart_used:
                    self._automatic_restart_used = True
                    self._ensure_worker_locked()
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

    def _record_overflow_locked(self, *, byte_count: int) -> None:
        self._dropped_records += 1
        self._dropped_bytes += byte_count
        self._pending_recovery.overflow_records += 1
        self._pending_recovery.overflow_bytes += byte_count

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


def _serialize_record(record: Mapping[str, Any], *, max_bytes: int | None = None) -> bytes:
    if not isinstance(record, Mapping):
        raise TypeError("event record must be a mapping")
    if max_bytes is not None:
        _admit_record_size(record, max_bytes)
    text = json.dumps(dict(record), ensure_ascii=True, separators=(",", ":"))
    data = text.encode("utf-8") + b"\n"
    if max_bytes is not None and len(data) > max_bytes:
        # The admission pass is deliberately conservative. Keep this fallback
        # so a future serializer change cannot violate the request-path bound.
        raise _RecordTooLarge("serialized event exceeds bounded admission")
    return data


def _admit_record_size(record: Mapping[str, Any], max_bytes: int) -> None:
    """Prove a JSONL record fits before allocating its serialized snapshot.

    ``json.dumps`` can allocate a string proportional to arbitrary request
    content. This pass measures exactly the JSON shape emitted by the writer
    and stops as soon as the configured byte budget is exhausted. It performs
    no JSON encoding or mapping snapshot until the record is known to fit.
    """

    if max_bytes < 3:
        raise _RecordTooLarge("JSONL record cannot fit in bounded admission")
    budget = _JsonSizeBudget(max_bytes - 1)
    _measure_json_mapping(record, budget, set())


class _JsonSizeBudget:
    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def consume(self, byte_count: int) -> None:
        if byte_count > self.remaining:
            raise _RecordTooLarge("JSON record exceeds bounded admission")
        self.remaining -= byte_count


def _measure_json_mapping(value: Mapping[Any, Any], budget: _JsonSizeBudget, active: set[int]) -> None:
    identity = id(value)
    if identity in active:
        raise ValueError("circular event mapping")
    active.add(identity)
    try:
        budget.consume(1)
        first = True
        for key in value:
            if not first:
                budget.consume(1)
            first = False
            _measure_json_key(key, budget)
            budget.consume(1)
            _measure_json_value(value[key], budget, active)
        budget.consume(1)
    finally:
        active.remove(identity)


def _measure_json_sequence(value: list[Any] | tuple[Any, ...], budget: _JsonSizeBudget, active: set[int]) -> None:
    identity = id(value)
    if identity in active:
        raise ValueError("circular event sequence")
    active.add(identity)
    try:
        budget.consume(1)
        first = True
        for item in value:
            if not first:
                budget.consume(1)
            first = False
            _measure_json_value(item, budget, active)
        budget.consume(1)
    finally:
        active.remove(identity)


def _measure_json_value(value: Any, budget: _JsonSizeBudget, active: set[int]) -> None:
    if value is None:
        budget.consume(4)
    elif isinstance(value, bool):
        budget.consume(4 if value else 5)
    elif isinstance(value, str):
        _measure_json_string(value, budget)
    elif isinstance(value, int):
        _measure_json_integer(value, budget)
    elif isinstance(value, float):
        _measure_json_float(value, budget)
    elif isinstance(value, dict):
        _measure_json_mapping(value, budget, active)
    elif isinstance(value, (list, tuple)):
        _measure_json_sequence(value, budget, active)
    else:
        raise TypeError("event record contains a non-JSON value")


def _measure_json_key(key: Any, budget: _JsonSizeBudget) -> None:
    if isinstance(key, str):
        _measure_json_string(key, budget)
    elif key is None:
        budget.consume(6)
    elif isinstance(key, bool):
        budget.consume(6 if key else 7)
    elif isinstance(key, int):
        _measure_json_integer(key, budget, quoted=True)
    elif isinstance(key, float):
        _measure_json_float(key, budget, quoted=True)
    else:
        raise TypeError("event record contains a non-JSON key")


def _measure_json_string(value: str, budget: _JsonSizeBudget) -> None:
    budget.consume(2)
    for character in value:
        code_point = ord(character)
        if character in {'"', "\\"}:
            budget.consume(2)
        elif character in {"\b", "\t", "\n", "\f", "\r"}:
            budget.consume(2)
        elif code_point < 0x20:
            budget.consume(6)
        elif code_point <= 0x7F:
            budget.consume(1)
        elif code_point <= 0xFFFF:
            budget.consume(6)
        else:
            budget.consume(12)


def _measure_json_integer(value: int, budget: _JsonSizeBudget, *, quoted: bool = False) -> None:
    sign = 1 if value < 0 else 0
    digit_upper_bound = 1 if value == 0 else (value.bit_length() * 30_103) // 100_000 + 2
    rendered_upper_bound = sign + digit_upper_bound
    if rendered_upper_bound > budget.remaining + 2:
        raise _RecordTooLarge("integer exceeds bounded admission")
    rendered_size = len(str(value))
    budget.consume(rendered_size + (2 if quoted else 0))


def _measure_json_float(value: float, budget: _JsonSizeBudget, *, quoted: bool = False) -> None:
    if math.isnan(value):
        rendered_size = 3
    elif math.isinf(value):
        rendered_size = 9 if value < 0 else 8
    else:
        rendered_size = len(float.__repr__(value))
    budget.consume(rendered_size + (2 if quoted else 0))


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
