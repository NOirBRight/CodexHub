from __future__ import annotations

from collections import deque
from collections.abc import Mapping
import errno
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Sequence
from unittest import TestCase
from unittest.mock import patch

from bounded_event_writer import BoundedEventWriter, JsonlFileSink


class WriterCrash(BaseException):
    pass


class RecursionErrorMapping(Mapping[str, object]):
    def __iter__(self):
        raise RecursionError("recursive mapping")

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> object:
        return "unused"


class RecordingSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.batches: list[list[bytes]] = []
        self.active_writes = 0
        self.max_active_writes = 0

    def append(self, records: Sequence[bytes]) -> None:
        with self._lock:
            self.active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self.active_writes)
        try:
            with self._lock:
                self.batches.append(list(records))
        finally:
            with self._lock:
                self.active_writes -= 1

    def records(self) -> list[dict[str, object]]:
        with self._lock:
            return [json.loads(record) for batch in self.batches for record in batch]


class BlockingSink(RecordingSink):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._block_once = True

    def append(self, records: Sequence[bytes]) -> None:
        should_block = False
        with self._lock:
            if self._block_once:
                self._block_once = False
                should_block = True
        if should_block:
            self.entered.set()
            self.release.wait(3)
        super().append(records)


class FailingSink(RecordingSink):
    def __init__(self, errors: Sequence[Exception]) -> None:
        super().__init__()
        self._errors: deque[Exception] = deque(errors)

    def append(self, records: Sequence[bytes]) -> None:
        if self._errors:
            raise self._errors.popleft()
        super().append(records)


class CrashWithRetainedBacklogSink(RecordingSink):
    def __init__(self) -> None:
        super().__init__()
        self.crash_entered = threading.Event()
        self.release_crash = threading.Event()
        self.replacement_wrote = threading.Event()
        self._crash_once = True

    def append(self, records: Sequence[bytes]) -> None:
        if self._crash_once:
            self._crash_once = False
            self.crash_entered.set()
            self.release_crash.wait(3)
            raise WriterCrash()
        super().append(records)
        self.replacement_wrote.set()


class AggregateFailureSink(BlockingSink):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_attempts = 0

    def append(self, records: Sequence[bytes]) -> None:
        if any(json.loads(record).get("event") == "telemetry_writer_recovered" for record in records):
            self.recovery_attempts += 1
            raise OSError(errno.ENOSPC, "disk full")
        super().append(records)


class PartialWriteHandle:
    def __init__(self, handle, state: dict[str, bool]) -> None:
        self._handle = handle
        self._state = state

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *args):
        return self._handle.__exit__(*args)

    def __getattr__(self, name: str):
        return getattr(self._handle, name)

    def write(self, data: bytes) -> int:
        if self._state["fail_once"]:
            self._state["fail_once"] = False
            count = max(1, len(data) // 2)
            self._handle.write(data[:count])
            self._handle.flush()
            raise OSError(errno.ENOSPC, "disk full")
        return self._handle.write(data)


class BoundedEventWriterTests(TestCase):
    def _writer(self, sink, **kwargs) -> BoundedEventWriter:
        kwargs.setdefault("max_records", 64)
        kwargs.setdefault("max_bytes", 64 * 1024)
        writer = BoundedEventWriter(sink, **kwargs)
        self.addCleanup(writer.shutdown, 1)
        return writer

    def test_bounds_record_and_serialized_byte_capacity(self) -> None:
        count_sink = BlockingSink()
        count_writer = self._writer(count_sink, max_records=2, max_bytes=4096)
        self.assertTrue(count_writer.enqueue({"event": "first"}))
        self.assertTrue(count_sink.entered.wait(1))
        self.assertTrue(count_writer.enqueue({"event": "second"}))
        self.assertFalse(count_writer.enqueue({"event": "third"}))
        self.assertEqual(count_writer.status().queued_records, 2)
        self.assertEqual(count_writer.status().dropped_records, 1)
        count_sink.release.set()
        self.assertTrue(count_writer.flush(1).completed)
        self.assertEqual(count_writer.status().recovery_events_written, 1)

        byte_sink = BlockingSink()
        first = {"event": "first", "payload": "a" * 32}
        second = {"event": "second", "payload": "b" * 32}
        first_size = len(json.dumps(first, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + 1
        second_size = len(json.dumps(second, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + 1
        byte_writer = self._writer(byte_sink, max_records=8, max_bytes=first_size + second_size - 1)
        self.assertTrue(byte_writer.enqueue(first))
        self.assertTrue(byte_sink.entered.wait(1))
        self.assertFalse(byte_writer.enqueue(second))
        self.assertEqual(byte_writer.status().queued_bytes, first_size)
        self.assertEqual(byte_writer.status().dropped_bytes, second_size)
        byte_sink.release.set()
        self.assertTrue(byte_writer.flush(1).completed)

    def test_oversized_single_record_is_rejected_before_full_json_serialization(self) -> None:
        sink = RecordingSink()
        writer = self._writer(sink, max_records=8, max_bytes=128)
        oversized = {"event": "oversized", "payload": "x" * 1_000_000}

        with patch("bounded_event_writer.json.dumps", wraps=json.dumps) as dumps:
            self.assertFalse(writer.enqueue(oversized))

        self.assertEqual(dumps.call_count, 0)
        status = writer.status()
        self.assertEqual(status.queued_records, 0)
        self.assertEqual(status.dropped_records, 1)
        self.assertEqual(status.dropped_bytes, 0)
        self.assertTrue(status.recovery_pending)

    def test_bounded_admission_preserves_escaped_json_record_bytes(self) -> None:
        record = {
            "event": "escaped",
            "value": '"\\\n\x01\u00e9\U0001f600',
            "nested": [None, True, False, -12, float("nan")],
        }
        expected = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
        sink = RecordingSink()
        writer = self._writer(sink, max_records=8, max_bytes=len(expected))

        self.assertTrue(writer.enqueue(record))
        self.assertTrue(writer.flush(1).completed)
        self.assertEqual(sink.batches, [[expected]])

    def test_accepted_records_keep_fifo_order_and_batch_after_slow_write(self) -> None:
        sink = BlockingSink()
        writer = self._writer(sink, batch_max_records=2, batch_max_bytes=4096)
        self.assertTrue(writer.enqueue({"event": "one"}))
        self.assertTrue(sink.entered.wait(1))
        self.assertTrue(writer.enqueue({"event": "two"}))
        self.assertTrue(writer.enqueue({"event": "three"}))

        sink.release.set()
        self.assertTrue(writer.flush(1).completed)

        self.assertEqual([record["event"] for record in sink.records()], ["one", "two", "three"])
        self.assertEqual([len(batch) for batch in sink.batches], [1, 2])

    def test_concurrent_producers_use_one_writer_and_complete_jsonl_records(self) -> None:
        sink = RecordingSink()
        writer = self._writer(sink, max_records=512, max_bytes=512 * 1024, batch_max_records=32)
        producer_count = 8
        records_per_producer = 25
        barrier = threading.Barrier(producer_count)
        accepted: list[bool] = []
        accepted_lock = threading.Lock()

        def produce(producer: int) -> None:
            barrier.wait()
            local = [writer.enqueue({"event": "request", "producer": producer, "index": index}) for index in range(records_per_producer)]
            with accepted_lock:
                accepted.extend(local)

        threads = [threading.Thread(target=produce, args=(producer,)) for producer in range(producer_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

        self.assertTrue(all(accepted))
        self.assertEqual(len(accepted), producer_count * records_per_producer)
        self.assertTrue(writer.flush(3).completed)
        records = sink.records()
        self.assertEqual(len(records), producer_count * records_per_producer)
        self.assertEqual(
            {(record["producer"], record["index"]) for record in records},
            {(producer, index) for producer in range(producer_count) for index in range(records_per_producer)},
        )
        self.assertEqual(sink.max_active_writes, 1)

    def test_a_sink_has_one_active_writer_and_can_be_reclaimed_after_shutdown(self) -> None:
        sink = RecordingSink()
        writer = self._writer(sink)
        with self.assertRaisesRegex(ValueError, "already owns this sink"):
            BoundedEventWriter(sink, max_records=64, max_bytes=64 * 1024)

        self.assertTrue(writer.shutdown(1).completed)
        replacement = self._writer(sink)
        self.assertTrue(replacement.enqueue({"event": "replacement"}))
        self.assertTrue(replacement.flush(1).completed)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "shared-events.jsonl"
            file_writer = self._writer(JsonlFileSink(path))
            with self.assertRaisesRegex(ValueError, "already owns this sink"):
                BoundedEventWriter(JsonlFileSink(path), max_records=64, max_bytes=64 * 1024)
            self.assertTrue(file_writer.shutdown(1).completed)

    def test_rotation_fence_holds_later_records_while_caller_policy_runs(self) -> None:
        sink = RecordingSink()
        writer = self._writer(sink)
        self.assertTrue(writer.enqueue({"event": "before-rotation"}))
        rotation_entered = threading.Event()
        release_rotation = threading.Event()
        results = []

        def rotate() -> None:
            rotation_entered.set()
            release_rotation.wait(1)

        rotation_thread = threading.Thread(target=lambda: results.append(writer.rotate(rotate, 1)))
        rotation_thread.start()
        self.assertTrue(rotation_entered.wait(1))
        self.assertTrue(writer.enqueue({"event": "after-rotation"}))
        self.assertEqual([record["event"] for record in sink.records()], ["before-rotation"])
        self.assertEqual(writer.status().queued_records, 1)

        release_rotation.set()
        rotation_thread.join(1)
        self.assertFalse(rotation_thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].completed)
        self.assertTrue(writer.flush(1).completed)
        self.assertEqual(
            [record["event"] for record in sink.records()],
            ["before-rotation", "after-rotation"],
        )

    def test_overflow_recovery_is_aggregate_and_never_recursively_enqueued(self) -> None:
        sink = AggregateFailureSink()
        writer = self._writer(sink, max_records=1, max_bytes=4096)
        self.assertTrue(writer.enqueue({"event": "accepted"}))
        self.assertTrue(sink.entered.wait(1))
        self.assertFalse(writer.enqueue({"event": "dropped"}))

        sink.release.set()
        result = writer.flush(1)

        self.assertEqual(result.outcome, "failed")
        self.assertEqual(sink.recovery_attempts, 1)
        self.assertEqual([record["event"] for record in sink.records()], ["accepted"])
        status = writer.status()
        self.assertEqual(status.accepted_records, 1)
        self.assertEqual(status.dropped_records, 1)
        self.assertTrue(status.recovery_pending)
        stopped = writer.shutdown(1)
        self.assertEqual(stopped.outcome, "failed")
        self.assertEqual(stopped.status.shutdown_state, "stopped")

    def test_slow_sink_does_not_block_enqueue_and_flush_times_out_boundedly(self) -> None:
        sink = BlockingSink()
        writer = self._writer(sink)
        started = time.monotonic()
        self.assertTrue(writer.enqueue({"event": "slow"}))
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(sink.entered.wait(1))

        result = writer.flush(0.02)
        self.assertEqual(result.outcome, "timeout")
        sink.release.set()
        self.assertTrue(writer.flush(1).completed)

    def test_permission_and_disk_full_failures_are_contained_and_recover(self) -> None:
        for error, expected_category in (
            (PermissionError("denied"), "permission_denied"),
            (OSError(errno.ENOSPC, "disk full"), "disk_full"),
        ):
            with self.subTest(expected_category=expected_category):
                sink = FailingSink([error])
                writer = self._writer(sink, clock=lambda: 123.0)
                self.assertTrue(writer.enqueue({"event": "will-fail"}))
                self.assertEqual(writer.flush(1).outcome, "failed")
                failed = writer.status()
                self.assertEqual(failed.last_failure_category, expected_category)
                self.assertEqual(failed.last_failure_time, 123.0)
                self.assertEqual(failed.dropped_records, 1)

                self.assertTrue(writer.enqueue({"event": "recovered"}))
                self.assertTrue(writer.flush(1).completed)
                events = [record["event"] for record in sink.records()]
                self.assertEqual(events, ["recovered", "telemetry_writer_recovered"])

    def test_serialization_rejection_and_repeated_sink_failures_recover_without_new_workers(self) -> None:
        serialization_sink = RecordingSink()
        serialization_writer = self._writer(serialization_sink)
        self.assertFalse(serialization_writer.enqueue({"event": "bad", "value": object()}))
        self.assertEqual(serialization_writer.flush(0).outcome, "failed")
        self.assertTrue(serialization_writer.enqueue({"event": "after-serialization"}))
        self.assertTrue(serialization_writer.flush(1).completed)
        self.assertEqual(
            [record["event"] for record in serialization_sink.records()],
            ["after-serialization", "telemetry_writer_recovered"],
        )

        recursive_sink = RecordingSink()
        recursive_writer = self._writer(recursive_sink)
        self.assertFalse(recursive_writer.enqueue(RecursionErrorMapping()))
        self.assertEqual(recursive_writer.status().last_failure_category, "serialization_rejected")

        nonfinite_sink = RecordingSink()
        nonfinite_writer = self._writer(nonfinite_sink)
        self.assertTrue(nonfinite_writer.enqueue({"event": "nonfinite", "value": float("nan")}))
        self.assertTrue(nonfinite_writer.flush(1).completed)
        self.assertIn(b'"value":NaN', nonfinite_sink.batches[0][0])

        sink = FailingSink([OSError(errno.EIO, "first"), OSError(errno.EIO, "second")])
        writer = self._writer(sink, batch_max_records=1)
        self.assertTrue(writer.enqueue({"event": "first"}))
        self.assertTrue(writer.enqueue({"event": "second"}))
        self.assertEqual(writer.flush(1).outcome, "failed")
        self.assertTrue(writer.enqueue({"event": "after-failures"}))
        self.assertTrue(writer.flush(1).completed)
        self.assertEqual(writer.status().writer_generation, 1)
        self.assertEqual(
            [record["event"] for record in sink.records()],
            ["after-failures", "telemetry_writer_recovered"],
        )

    def test_partial_disk_write_is_repaired_before_future_complete_jsonl_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "events.jsonl"
            state = {"fail_once": True}

            def open_file(open_path: Path, mode: str):
                handle = open_path.open(mode)
                if state["fail_once"]:
                    return PartialWriteHandle(handle, state)
                return handle

            writer = self._writer(
                JsonlFileSink(path, open_file=open_file),
                recovery_record_factory=lambda summary: {
                    "event": "telemetry_writer_recovered",
                    "failed_records": summary.failed_records,
                },
            )
            self.assertTrue(writer.enqueue({"event": "partial"}))
            self.assertEqual(writer.flush(1).outcome, "failed")
            self.assertTrue(writer.enqueue({"event": "after-repair"}))
            self.assertTrue(writer.flush(1).completed)

            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["event"] for record in records], ["after-repair", "telemetry_writer_recovered"])

    def test_writer_crash_restarts_once_and_drains_retained_backlog_without_followup_calls(self) -> None:
        sink = CrashWithRetainedBacklogSink()
        writer = self._writer(sink)
        self.assertTrue(writer.enqueue({"event": "crash"}))
        self.assertTrue(sink.crash_entered.wait(1))
        self.assertTrue(writer.enqueue({"event": "retained"}))
        sink.release_crash.set()

        self.assertTrue(sink.replacement_wrote.wait(1))
        self.assertEqual(writer.status().writer_generation, 2)
        self.assertEqual(
            [record["event"] for record in sink.records()],
            ["retained", "telemetry_writer_recovered"],
        )

    def test_shutdown_drains_or_times_out_without_hanging(self) -> None:
        normal_sink = RecordingSink()
        normal_writer = self._writer(normal_sink)
        self.assertTrue(normal_writer.enqueue({"event": "before-stop"}))
        result = normal_writer.shutdown(1)
        self.assertTrue(result.completed)
        self.assertEqual(result.status.shutdown_state, "stopped")
        self.assertFalse(normal_writer.enqueue({"event": "after-stop"}))

        slow_sink = BlockingSink()
        slow_writer = self._writer(slow_sink)
        self.assertTrue(slow_writer.enqueue({"event": "slow-stop"}))
        self.assertTrue(slow_sink.entered.wait(1))
        self.assertEqual(slow_writer.shutdown(0.02).outcome, "timeout")
        slow_sink.release.set()
        self.assertTrue(slow_writer.shutdown(1).completed)
