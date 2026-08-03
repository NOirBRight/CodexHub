from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest import TestCase
from unittest.mock import patch

import diagnostic_control
import diagnostic_recorder


class FakeClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _RecordingRecorder:
    def __init__(self, recorder: diagnostic_recorder.DiagnosticRecorder) -> None:
        self._recorder = recorder
        self.calls = 0
        self._lock = threading.Lock()

    def status(self) -> object:
        return self._recorder.status()

    def mark_incident(self, category: str = "manual") -> str | None:
        with self._lock:
            self.calls += 1
        return self._recorder.mark_incident(category)

    def pause(self) -> object:
        return self._recorder.pause()

    def resume(self) -> object:
        return self._recorder.resume()

    def delete_incident(self, incident_id: str) -> bool:
        return self._recorder.delete_incident(incident_id)


class _BlockingMarkRecorder(_RecordingRecorder):
    def __init__(self, recorder: diagnostic_recorder.DiagnosticRecorder) -> None:
        super().__init__(recorder)
        self.mark_entered = threading.Event()
        self.duplicate_mark_entered = threading.Event()
        self.release_mark = threading.Event()

    def mark_incident(self, category: str = "manual") -> str | None:
        with self._lock:
            self.calls += 1
            duplicate = self.calls > 1
        self.mark_entered.set()
        if duplicate:
            self.duplicate_mark_entered.set()
        self.release_mark.wait(2)
        return self._recorder.mark_incident(category)


class _BlockingDeleteRecorder(_RecordingRecorder):
    def __init__(self, recorder: diagnostic_recorder.DiagnosticRecorder) -> None:
        super().__init__(recorder)
        self.delete_entered = threading.Event()
        self.duplicate_delete_entered = threading.Event()
        self.release_delete = threading.Event()
        self.delete_calls = 0

    def delete_incident(self, incident_id: str) -> bool:
        with self._lock:
            self.delete_calls += 1
            duplicate = self.delete_calls > 1
        self.delete_entered.set()
        if duplicate:
            self.duplicate_delete_entered.set()
        self.release_delete.wait(2)
        return self._recorder.delete_incident(incident_id)


class _BlockingPauseRecorder(_RecordingRecorder):
    def __init__(self, recorder: diagnostic_recorder.DiagnosticRecorder) -> None:
        super().__init__(recorder)
        self.pause_entered = threading.Event()
        self.release_pause = threading.Event()
        self.duplicate_pause_entered = threading.Event()
        self.pause_calls = 0
        self.resume_calls = 0
        self.status_calls = 0

    def status(self) -> object:
        with self._lock:
            self.status_calls += 1
        return self._recorder.status()

    def pause(self) -> object:
        with self._lock:
            self.pause_calls += 1
            if self.pause_calls > 1:
                self.duplicate_pause_entered.set()
        self.pause_entered.set()
        self.release_pause.wait(2)
        return self._recorder.pause()

    def resume(self) -> object:
        with self._lock:
            self.resume_calls += 1
        return self._recorder.resume()


class DiagnosticControlTests(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = FakeClock()
        self.recorder = diagnostic_recorder.DiagnosticRecorder(
            self.root,
            clock=self.clock,
            incident_tail_seconds=1,
        )
        self.bridge = diagnostic_control.DiagnosticControlBridge(
            self.recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.bridge.start()
        self.addCleanup(self.bridge.shutdown)
        self.addCleanup(self.recorder.shutdown, 1)
        self._counter = 0

    def _request(self, operation: str, **extra: object) -> dict[str, object]:
        self._counter += 1
        request_id = f"c{self._counter:016x}"
        request = {
            "schema_version": 1,
            "request_id": request_id,
            "operation": operation,
            "expires_at_ms": int(self.clock() * 1000) + 5_000,
            **extra,
        }
        path = self.root / "diagnostics" / "control" / "requests" / f"{request_id}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        self.bridge.process_once()
        response_path = self.root / "diagnostics" / "control" / "responses" / f"{request_id}.json"
        self.assertTrue(response_path.is_file())
        return json.loads(response_path.read_text(encoding="utf-8"))

    def test_status_pause_resume_and_mark_use_a_content_free_versioned_contract(self) -> None:
        status = self._request("status")

        self.assertTrue(status["ok"])
        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["status"]["flavor"], "debug")
        self.assertNotIn("records", status)
        self.assertNotIn("payload", json.dumps(status))

        paused = self._request("pause")
        self.assertTrue(paused["ok"])
        self.assertTrue(paused["status"]["paused"])

        resumed = self._request("resume")
        self.assertTrue(resumed["ok"])
        self.assertTrue(resumed["status"]["active"])

        marked = self._request("mark")
        self.assertTrue(marked["ok"])
        self.assertEqual(marked["result"], {"accepted": True, "incident_id": "i000001"})

    def test_delete_and_status_are_deterministic_after_the_tail_freezes(self) -> None:
        # Drive the freeze synchronously below.  The production recorder starts
        # a maintenance thread after a marker is created; with the fake clock,
        # that thread can race the explicit process_due_incidents() call and
        # either freeze twice or advance the incident counter before the
        # response is read.
        with patch.object(diagnostic_recorder.DiagnosticRecorder, "_ensure_control_thread_locked"):
            marked = self._request("mark")
        self.assertEqual(marked["result"]["incident_id"], "i000001")
        self.clock.advance(1)
        self.assertEqual(self.recorder.process_due_incidents(), 1)

        status = self._request("status")
        self.assertEqual(status["status"]["incident_ids"], ["i000001"])

        deleted = self._request("delete", incident_id="i000001")
        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["result"], {"deleted": True})
        self.assertEqual(self._request("status")["status"]["incident_ids"], [])

    def test_unknown_fields_and_expired_requests_fail_closed_without_recording(self) -> None:
        request_id = "c00000000000000ff"
        request = {
            "schema_version": 1,
            "request_id": request_id,
            "operation": "mark",
            "expires_at_ms": int(self.clock() * 1000) - 1,
            "private_payload": "must-not-survive",
        }
        path = self.root / "diagnostics" / "control" / "requests" / f"{request_id}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        self.bridge.process_once()

        response_path = self.root / "diagnostics" / "control" / "responses" / f"{request_id}.json"
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertFalse(response["ok"])
        self.assertEqual(response["code"], "invalid_request")
        self.assertIsNone(self.recorder.status().last_marker_category)
        self.assertNotIn("must-not-survive", response_path.read_text(encoding="utf-8"))

    def test_concurrent_process_once_does_not_duplicate_mark(self) -> None:
        self.bridge.shutdown()
        recorder = _BlockingMarkRecorder(self.recorder)
        bridge = diagnostic_control.DiagnosticControlBridge(
            recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.addCleanup(bridge.shutdown)
        self.addCleanup(recorder.release_mark.set)
        request_id = "c0000000000000100"
        request_dir = self.root / "diagnostics" / "control" / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        (request_dir / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "operation": "mark",
                    "expires_at_ms": int(self.clock() * 1000) + 5_000,
                }
            ),
            encoding="utf-8",
        )

        caller_barrier = threading.Barrier(3)

        def process_batch() -> None:
            caller_barrier.wait()
            bridge.process_once()

        callers = [threading.Thread(target=process_batch) for _ in range(2)]
        for caller in callers:
            caller.start()
        caller_barrier.wait()
        self.assertTrue(recorder.mark_entered.wait(1))
        self.assertFalse(recorder.duplicate_mark_entered.wait(0.1))
        recorder.release_mark.set()
        for caller in callers:
            caller.join(2)

        self.assertTrue(all(not caller.is_alive() for caller in callers))
        self.assertEqual(recorder.calls, 1)
        response_path = (
            self.root
            / "diagnostics"
            / "control"
            / "responses"
            / f"{request_id}.json"
        )
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["result"]["incident_id"], "i000001")

    def test_concurrent_process_once_does_not_double_delete_or_overwrite_response(self) -> None:
        self.bridge.shutdown()
        with patch.object(diagnostic_recorder.DiagnosticRecorder, "_ensure_control_thread_locked"):
            incident_id = self.recorder.mark_incident("manual")
        self.assertEqual(incident_id, "i000001")
        self.clock.advance(1)
        self.assertEqual(self.recorder.process_due_incidents(), 1)

        recorder = _BlockingDeleteRecorder(self.recorder)
        bridge = diagnostic_control.DiagnosticControlBridge(
            recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.addCleanup(bridge.shutdown)
        self.addCleanup(recorder.release_delete.set)
        request_id = "c0000000000000200"
        request_dir = self.root / "diagnostics" / "control" / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        request_path = request_dir / f"{request_id}.json"
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "operation": "delete",
                    "expires_at_ms": int(self.clock() * 1000) + 5_000,
                    "incident_id": incident_id,
                }
            ),
            encoding="utf-8",
        )

        caller_barrier = threading.Barrier(3)

        def process_batch() -> None:
            caller_barrier.wait()
            bridge.process_once()

        callers = [threading.Thread(target=process_batch) for _ in range(2)]
        for caller in callers:
            caller.start()
        caller_barrier.wait()
        self.assertTrue(recorder.delete_entered.wait(1))
        self.assertFalse(recorder.duplicate_delete_entered.wait(0.1))
        recorder.release_delete.set()
        for caller in callers:
            caller.join(2)

        self.assertTrue(all(not caller.is_alive() for caller in callers))
        self.assertEqual(recorder.delete_calls, 1)
        response_path = (
            self.root
            / "diagnostics"
            / "control"
            / "responses"
            / f"{request_id}.json"
        )
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["result"], {"deleted": True})
        self.assertFalse(request_path.exists())

    def test_daemon_and_manual_batches_serialize_pause_resume_and_status(self) -> None:
        self.bridge.shutdown()
        recorder = _BlockingPauseRecorder(self.recorder)
        bridge = diagnostic_control.DiagnosticControlBridge(
            recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.addCleanup(bridge.shutdown)
        self.addCleanup(recorder.release_pause.set)
        request_dir = self.root / "diagnostics" / "control" / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        request_ids = {
            "pause": "c0000000000000300",
            "resume": "c0000000000000301",
            "status": "c0000000000000302",
        }
        for operation, request_id in request_ids.items():
            (request_dir / f"{request_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "request_id": request_id,
                        "operation": operation,
                        "expires_at_ms": int(self.clock() * 1000) + 5_000,
                    }
                ),
                encoding="utf-8",
            )

        bridge.start()
        self.assertTrue(recorder.pause_entered.wait(1))
        processed: list[int] = []
        manual = threading.Thread(target=lambda: processed.append(bridge.process_once()))
        manual.start()
        self.assertFalse(recorder.duplicate_pause_entered.wait(0.1))
        recorder.release_pause.set()
        manual.join(2)
        bridge.shutdown()

        self.assertFalse(manual.is_alive())
        self.assertEqual(recorder.pause_calls, 1)
        self.assertEqual(recorder.resume_calls, 1)
        self.assertEqual(recorder.status_calls, 9)
        self.assertEqual(processed, [0])
        response_dir = self.root / "diagnostics" / "control" / "responses"
        self.assertEqual(
            sorted(path.stem for path in response_dir.glob("*.json")),
            sorted(request_ids.values()),
        )
        self.assertFalse(any(request_dir.glob("*.json")))

    def test_shutdown_is_bounded_while_a_batch_owns_the_serialization_boundary(self) -> None:
        self.bridge.shutdown()
        recorder = _BlockingPauseRecorder(self.recorder)
        bridge = diagnostic_control.DiagnosticControlBridge(
            recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.addCleanup(bridge.shutdown)
        self.addCleanup(recorder.release_pause.set)
        request_id = "c0000000000000400"
        request_dir = self.root / "diagnostics" / "control" / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        (request_dir / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "operation": "pause",
                    "expires_at_ms": int(self.clock() * 1000) + 5_000,
                }
            ),
            encoding="utf-8",
        )

        bridge.start()
        self.assertTrue(recorder.pause_entered.wait(1))
        started_at = time.monotonic()
        bridge.shutdown(timeout=0.02)
        elapsed = time.monotonic() - started_at
        self.assertLess(elapsed, 0.25)
        recorder.release_pause.set()
        bridge.shutdown(timeout=1)

    def test_malformed_request_is_removed_and_does_not_block_later_valid_work(self) -> None:
        self.bridge.shutdown()
        recorder = _RecordingRecorder(self.recorder)
        bridge = diagnostic_control.DiagnosticControlBridge(
            recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.addCleanup(bridge.shutdown)
        request_dir = self.root / "diagnostics" / "control" / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        malformed_path = request_dir / "c0000000000000500.json"
        malformed_path.write_text("{", encoding="utf-8")
        unknown_id = "c0000000000000501"
        (request_dir / f"{unknown_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": unknown_id,
                    "operation": "unrecognized",
                    "expires_at_ms": int(self.clock() * 1000) + 5_000,
                }
            ),
            encoding="utf-8",
        )
        request_id = "c0000000000000502"
        (request_dir / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "operation": "mark",
                    "expires_at_ms": int(self.clock() * 1000) + 5_000,
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(bridge.process_once(), 3)
        self.assertFalse(malformed_path.exists())
        unknown_response = json.loads(
            (
                self.root
                / "diagnostics"
                / "control"
                / "responses"
                / f"{unknown_id}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(unknown_response["code"], "invalid_request")
        response_path = (
            self.root
            / "diagnostics"
            / "control"
            / "responses"
            / f"{request_id}.json"
        )
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["result"]["incident_id"], "i000001")

    def test_failed_claim_cleanup_cannot_repeat_an_accepted_request(self) -> None:
        self.bridge.shutdown()
        recorder = _RecordingRecorder(self.recorder)
        bridge = diagnostic_control.DiagnosticControlBridge(
            recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.addCleanup(bridge.shutdown)
        request_id = "c0000000000000700"
        request_dir = self.root / "diagnostics" / "control" / "requests"
        request_dir.mkdir(parents=True, exist_ok=True)
        (request_dir / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "operation": "mark",
                    "expires_at_ms": int(self.clock() * 1000) + 5_000,
                }
            ),
            encoding="utf-8",
        )
        original_unlink = Path.unlink

        def fail_claim_cleanup(path: Path, *, missing_ok: bool = False) -> None:
            if path.name.endswith(".processing"):
                raise OSError("simulated claim cleanup failure")
            original_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", fail_claim_cleanup):
            self.assertEqual(bridge.process_once(), 1)
            self.assertEqual(bridge.process_once(), 0)

        self.assertEqual(recorder.calls, 1)
        processing = list(request_dir.glob("*.processing"))
        self.assertEqual(len(processing), 1)
        original_unlink(processing[0], missing_ok=True)

    def test_response_retention_runs_inside_the_serialized_batch(self) -> None:
        self.bridge.shutdown()
        recorder = _BlockingMarkRecorder(self.recorder)
        bridge = diagnostic_control.DiagnosticControlBridge(
            recorder,
            self.root,
            clock=self.clock,
            poll_interval_seconds=60,
        )
        self.addCleanup(bridge.shutdown)
        self.addCleanup(recorder.release_mark.set)
        request_id = "c0000000000000600"
        request_dir = self.root / "diagnostics" / "control" / "requests"
        response_dir = self.root / "diagnostics" / "control" / "responses"
        request_dir.mkdir(parents=True, exist_ok=True)
        response_dir.mkdir(parents=True, exist_ok=True)
        (request_dir / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "operation": "mark",
                    "expires_at_ms": int(self.clock() * 1000) + 5_000,
                }
            ),
            encoding="utf-8",
        )
        expired = response_dir / "c0000000000000601.json"
        expired.write_text("{}", encoding="utf-8")
        expired_at = self.clock() - diagnostic_control.CONTROL_RESPONSE_RETENTION_SECONDS - 1
        os.utime(expired, (expired_at, expired_at))

        caller_barrier = threading.Barrier(3)

        def process_batch() -> None:
            caller_barrier.wait()
            bridge.process_once()

        callers = [threading.Thread(target=process_batch) for _ in range(2)]
        for caller in callers:
            caller.start()
        caller_barrier.wait()
        self.assertTrue(recorder.mark_entered.wait(1))
        self.assertFalse(recorder.duplicate_mark_entered.wait(0.1))
        recorder.release_mark.set()
        for caller in callers:
            caller.join(2)

        self.assertTrue(all(not caller.is_alive() for caller in callers))
        self.assertEqual(recorder.calls, 1)
        self.assertFalse(expired.exists())
        response_path = response_dir / f"{request_id}.json"
        self.assertTrue(response_path.is_file())
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["result"]["incident_id"], "i000001")
