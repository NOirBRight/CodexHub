"""Bounded, non-blocking JSONL event writing.

Callers hand this module already-sanitized mappings. ``enqueue`` serializes a
snapshot in memory and never opens, writes, flushes, rotates, or waits on a
sink. One background writer owns batching, sink failures, and JSONL recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import logging
from logging.handlers import QueueHandler, QueueListener
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any, BinaryIO, Callable, Literal, Mapping, Protocol, Sequence
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


class _EventQueueHandler(QueueHandler):
    """QueueHandler variant that keeps the already-serialized payload intact."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


class _JsonlHandler(logging.Handler):
    def __init__(self, writer: "BoundedEventWriter") -> None:
        super().__init__()
        self._writer = writer

    def emit(self, record: logging.LogRecord) -> None:
        self._writer._write_batch(record.records)


class _EventQueueListener(QueueListener):
    """QueueListener with bounded JSONL batching and a timed stop."""

    def __init__(
        self, writer: "BoundedEventWriter", event_queue: queue.Queue[logging.LogRecord]
    ) -> None:
        super().__init__(event_queue, _JsonlHandler(writer), respect_handler_level=True)
        self._writer = writer

    def _monitor(self) -> None:
        pending: logging.LogRecord | None = None
        try:
            while True:
                record = pending or self.dequeue(True)
                pending = None
                if record is self._sentinel:
                    if hasattr(self.queue, "task_done"):
                        self.queue.task_done()
                    break

                first: _QueuedRecord = record.queued_record
                self._writer._wait_for_rotation_fence(first.sequence)
                batch = [first]
                batch_bytes = first.byte_count
                while len(batch) < self._writer._batch_max_records:
                    try:
                        candidate = self.dequeue(False)
                    except queue.Empty:
                        break
                    if candidate is self._sentinel:
                        pending = candidate
                        break
                    item: _QueuedRecord = candidate.queued_record
                    if self._writer._rotation_blocks(item.sequence) or (
                        batch_bytes + item.byte_count > self._writer._batch_max_bytes
                    ):
                        pending = candidate
                        break
                    batch.append(item)
                    batch_bytes += item.byte_count

                batch_record = logging.LogRecord(
                    name="bounded_event_writer",
                    level=logging.INFO,
                    pathname=__file__,
                    lineno=0,
                    msg="",
                    args=(),
                    exc_info=None,
                )
                batch_record.records = batch
                self.handle(batch_record)
                for _item in batch:
                    if hasattr(self.queue, "task_done"):
                        self.queue.task_done()
        finally:
            self._writer._listener_stopped()

    def stop_bounded(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        self.enqueue_sentinel()
        thread.join(max(0.0, timeout))
        if thread.is_alive():
            return False
        self._thread = None
        return True


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
            self.failure_categories[category] = (
                self.failure_categories.get(category, 0) + 1
            )


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
        return (
            isinstance(other, JsonlFileSink)
            and self._identity_path == other._identity_path
        )

    @staticmethod
    def _default_open_file(path: Path, mode: str) -> BinaryIO:
        return path.open(mode)

    def append(self, records: Sequence[bytes]) -> None:
        if not records:
            return
        if any(
            not record.endswith(b"\n") or b"\n" in record[:-1] for record in records
        ):
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
        self._recovery_record_factory = (
            recovery_record_factory or _default_recovery_record
        )
        self._clock = clock
        self._thread_name = thread_name

        self._condition = threading.Condition(threading.RLock())
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue()
        self._queue_handler = _EventQueueHandler(self._queue)
        self._listener = _EventQueueListener(self, self._queue)
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
        self._writer_generation = 1
        self._rotation_target_sequence: int | None = None
        self._sink_ownership: Literal["weak", "strong"]
        self._claim_sink_ownership()
        try:
            self._listener.start()
            if self._listener._thread is not None:
                self._listener._thread.name = self._thread_name
        except Exception:
            self._writer_generation = 0
            self._record_failure_locked(
                "writer_start_failed", record_count=0, byte_count=0
            )

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
                self._record_failure_locked(
                    "serialization_rejected", record_count=1, byte_count=0
                )
                self._condition.notify_all()
            return False

        byte_count = len(data)
        with self._condition:
            if self._shutdown_state != "running":
                self._dropped_records += 1
                self._dropped_bytes += byte_count
                self._condition.notify_all()
                return False
            if (
                self._pending_records >= self._max_records
                or self._pending_bytes + byte_count > self._max_bytes
            ):
                self._record_overflow_locked(byte_count=byte_count)
                self._condition.notify_all()
                return False

            queued = _QueuedRecord(sequence=self._next_sequence, data=data)
            self._next_sequence += 1
            log_record = logging.LogRecord(
                name="bounded_event_writer",
                level=logging.INFO,
                pathname=__file__,
                lineno=0,
                msg="",
                args=(),
                exc_info=None,
            )
            log_record.queued_record = queued
            try:
                self._queue_handler.handle(log_record)
            except (
                queue.Full
            ):  # pragma: no cover - the queue is intentionally unbounded.
                self._record_overflow_locked(byte_count=byte_count)
                return False
            self._pending_records += 1
            self._pending_bytes += byte_count
            self._accepted_records += 1
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
            self._condition.notify_all()
        return self._wait_for(target_sequence, timeout)

    def rotate(
        self, operation: RotationOperation, timeout: float = 5.0
    ) -> BoundedEventWriterResult:
        """Fence sink writes around a caller-owned, bounded rotation operation.

        The caller owns the rotation policy and its file changes. This module
        drains records accepted before the fence, holds later records in memory
        without blocking their enqueuers, then releases them only after the
        supplied operation returns.
        """

        with self._condition:
            if (
                self._shutdown_state != "running"
                or self._rotation_target_sequence is not None
            ):
                return BoundedEventWriterResult("failed", self._status_locked())
            target_sequence = self._next_sequence - 1
            self._rotation_target_sequence = target_sequence
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
                self._record_failure_locked(
                    "rotation_failed", record_count=0, byte_count=0
                )
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
            self._condition.notify_all()

        result = self._wait_for(target_sequence, timeout)
        if result.outcome == "timeout":
            return result

        deadline = time.monotonic() + max(0.0, timeout)
        stopped = self._listener.stop_bounded(max(0.0, deadline - time.monotonic()))

        release_sink_ownership = False
        with self._condition:
            if not stopped:
                return BoundedEventWriterResult("timeout", self._status_locked())
            if self._pending_records == 0:
                self._shutdown_state = "stopped"
                release_sink_ownership = True
            result = self._result_locked()
        if release_sink_ownership:
            self._release_sink_ownership()
        return result

    def _wait_for(
        self, target_sequence: int, timeout: float
    ) -> BoundedEventWriterResult:
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

    def _rotation_blocks(self, sequence: int) -> bool:
        with self._condition:
            return (
                self._rotation_target_sequence is not None
                and sequence > self._rotation_target_sequence
            )

    def _wait_for_rotation_fence(self, sequence: int) -> None:
        with self._condition:
            while (
                self._rotation_target_sequence is not None
                and sequence > self._rotation_target_sequence
            ):
                self._condition.wait()

    def _write_batch(self, inflight: Sequence[_QueuedRecord]) -> None:
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
            return
        except BaseException:
            with self._condition:
                self._complete_batch_locked(inflight)
                self._record_failure_locked(
                    "writer_crash",
                    record_count=len(inflight),
                    byte_count=sum(record.byte_count for record in inflight),
                )
                self._condition.notify_all()
            return

        with self._condition:
            self._complete_batch_locked(inflight)
            self._written_records += len(inflight)
            recovery = (
                self._pending_recovery.snapshot()
                if self._pending_recovery.has_data()
                else None
            )
            self._recovery_inflight = recovery is not None
            self._condition.notify_all()
        if recovery is not None:
            self._emit_recovery(recovery)

    def _listener_stopped(self) -> None:
        with self._condition:
            self._condition.notify_all()

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
            recovery_pending=self._pending_recovery.has_data()
            or self._recovery_inflight,
            shutdown_state=self._shutdown_state,
            writer_alive=self._listener._thread is not None
            and self._listener._thread.is_alive(),
            writer_generation=self._writer_generation,
        )


def _serialize_record(
    record: Mapping[str, Any], *, max_bytes: int | None = None
) -> bytes:
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


def _measure_json_mapping(
    value: Mapping[Any, Any], budget: _JsonSizeBudget, active: set[int]
) -> None:
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


def _measure_json_sequence(
    value: list[Any] | tuple[Any, ...], budget: _JsonSizeBudget, active: set[int]
) -> None:
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


def _measure_json_integer(
    value: int, budget: _JsonSizeBudget, *, quoted: bool = False
) -> None:
    sign = 1 if value < 0 else 0
    digit_upper_bound = (
        1 if value == 0 else (value.bit_length() * 30_103) // 100_000 + 2
    )
    rendered_upper_bound = sign + digit_upper_bound
    if rendered_upper_bound > budget.remaining + 2:
        raise _RecordTooLarge("integer exceeds bounded admission")
    rendered_size = len(str(value))
    budget.consume(rendered_size + (2 if quoted else 0))


def _measure_json_float(
    value: float, budget: _JsonSizeBudget, *, quoted: bool = False
) -> None:
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
