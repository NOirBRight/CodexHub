from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import TestCase

import diagnostic_control
import diagnostic_recorder


class FakeClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


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
