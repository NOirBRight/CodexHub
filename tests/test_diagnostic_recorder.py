from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
from unittest import TestCase
from unittest.mock import patch

import diagnostic_recorder


class FakeClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class DiagnosticRecorderTests(TestCase):
    def _root(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _recorder(self, root: Path, clock: FakeClock, **kwargs) -> diagnostic_recorder.DiagnosticRecorder:
        recorder = diagnostic_recorder.DiagnosticRecorder(root, clock=clock, **kwargs)
        self.addCleanup(recorder.shutdown, 1)
        return recorder

    def _deterministic_recorder(
        self,
        root: Path,
        clock: FakeClock,
        **kwargs,
    ) -> diagnostic_recorder.DiagnosticRecorder:
        """Construct without the daemon so a fake-clock fixture owns freezing."""

        with patch.object(diagnostic_recorder.DiagnosticRecorder, "_ensure_control_thread_locked"):
            recorder = self._recorder(root, clock, **kwargs)
        recorder._ensure_control_thread_locked = lambda: None
        return recorder

    @staticmethod
    def _rolling_records(root: Path) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for path in sorted((root / "diagnostics" / "rolling").glob("*.jsonl")):
            records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
        return records

    def test_normal_flavor_has_no_runtime_recorder(self) -> None:
        root = self._root()
        recorder = diagnostic_recorder.for_compile_flavor(root, "normal")

        recorder.observe_sse_line("raw-request", 12)

        self.assertIsInstance(recorder, diagnostic_recorder.DisabledDiagnosticRecorder)
        self.assertFalse(recorder.status().active)
        self.assertFalse((root / "diagnostics").exists())

    def test_allow_list_drops_private_content_and_coalesces_long_streams(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._recorder(root, clock)
        raw_request = "request-secret-8a3e"
        recorder.record_phase(
            raw_request,
            "downstream_accept",
            provider="official",
            model="openai/gpt-5.6",
            route="official",
            status=200,
            prompt="do not retain this prompt",
            authorization="Bearer secret-token",
            path="C:/Users/private/project",
            unknown_payload={"private": "must-not-survive"},
            provider_hint=["untrusted-provider"],
        )
        recorder.observe_upstream_headers(
            raw_request,
            status=200,
            headers={
                "Authorization": "Bearer secret-token",
                "Cookie": "session=private",
                "Content-Type": "text/event-stream; charset=utf-8",
                "Content-Length": "9999",
            },
        )
        for _ in range(200_000):
            recorder.observe_sse_line(raw_request, 23)
        recorder.observe_terminal(raw_request, forwarded=False)
        recorder.observe_terminal(raw_request, forwarded=True)
        self.assertTrue(recorder.flush(3))

        records = self._rolling_records(root)
        rendered = json.dumps(records, ensure_ascii=True)
        for forbidden in (
            raw_request,
            "secret-token",
            "session=private",
            "do not retain this prompt",
            "C:/Users/private/project",
            "must-not-survive",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(sum(record["kind"] == "sse_first" for record in records), 1)
        self.assertLessEqual(sum(record["kind"] == "sse_checkpoint" for record in records), 16)
        self.assertTrue(all("prompt" not in record and "authorization" not in record for record in records))
        self.assertEqual(
            next(record for record in records if record["kind"] == "upstream_headers")["content_type_class"],
            "event-stream",
        )
        self.assertTrue(all(str(record.get("request", "")).startswith("r") for record in records if "request" in record))

    def test_sanitized_61s_to_300s_stream_to_downstream_close_ordering(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._recorder(root, clock)
        raw_request = "private-request-61s-300s"

        clock.advance(61)
        recorder.observe_upstream_attempt(
            raw_request,
            attempt=1,
            retry_budget=2,
            elapsed_ms=61_000,
            outcome="ok",
            connection_disposition="unobserved",
            provider="official",
            model="openai/gpt-5.6",
        )
        clock.advance(239)
        recorder.observe_upstream_headers(
            raw_request,
            status=200,
            headers={"Content-Type": "text/event-stream", "Authorization": "Bearer private"},
        )
        recorder.observe_sse_line(raw_request, 73)
        recorder.observe_close(
            raw_request,
            side="downstream",
            outcome="error",
            status=499,
            elapsed_ms=300_000,
            automatic_marker=False,
        )
        self.assertTrue(recorder.flush(3))

        records = self._rolling_records(root)
        self.assertEqual(
            [record["kind"] for record in records],
            ["upstream_attempt", "upstream_headers", "sse_first", "downstream_close"],
        )
        self.assertEqual([record["seq"] for record in records], sorted(record["seq"] for record in records))
        self.assertEqual(records[0]["elapsed_ms"], 61_000)
        self.assertEqual(records[-1]["elapsed_ms"], 300_000)
        self.assertEqual(records[-1]["lines"], 1)
        self.assertNotIn(raw_request, json.dumps(records, ensure_ascii=True))

    def test_rotation_evicts_oldest_complete_segments_for_time_and_bytes(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._deterministic_recorder(
            root,
            clock,
            rolling_window_seconds=10,
            rolling_max_bytes=1024,
            max_segment_bytes=512,
            segment_seconds=1,
        )
        for index in range(40):
            recorder.record_phase(
                f"request-{index}",
                "request_complete",
                provider="official",
                model="openai/gpt-5.6",
                elapsed_ms=index,
                status=200,
                outcome="ok",
            )
            clock.advance(1.1)
        self.assertTrue(recorder.flush(3))
        status = recorder.status()
        self.assertLessEqual(status.rolling_bytes, 1024)
        self.assertGreater(status.rolling_evicted_segments, 0)
        self.assertTrue(status.truncated)

        clock.advance(20)
        recorder.record_phase("latest", "request_complete", status=200, outcome="ok")
        self.assertTrue(recorder.flush(3))
        records = self._rolling_records(root)
        self.assertEqual([record["request"] for record in records], ["r000041"])

    def test_recovery_enforces_the_byte_cap_before_the_next_write(self) -> None:
        clock = FakeClock()
        root = self._root()
        rolling = root / "diagnostics" / "rolling"
        rolling.mkdir(parents=True)
        for ordinal in range(1, 4):
            (rolling / f"segment-{ordinal:08d}-1700000000000.jsonl").write_bytes(b"x" * 399 + b"\n")

        recorder = self._recorder(
            root,
            clock,
            rolling_max_bytes=1024,
            max_segment_bytes=512,
        )

        self.assertLessEqual(recorder.status().rolling_bytes, 1024)
        self.assertGreaterEqual(recorder.status().rolling_evicted_segments, 1)

    def test_freeze_retention_and_read_only_artifact_contract(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._recorder(root, clock, incident_tail_seconds=1, max_incidents=3)
        recorder.record_phase("request-a", "downstream_close", outcome="error", status=499)
        recorder.record_phase("request-a", "upstream_close", outcome="error", status=502)
        with patch.object(recorder, "_ensure_control_thread_locked"):
            incident = recorder.mark_incident("manual")
        self.assertEqual(incident, "i000001")
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 1)

        artifact = recorder.read_incident("i000001")
        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertEqual(artifact["manifest"]["schema_version"], 1)
        self.assertEqual(artifact["manifest"]["classification"], "downstream-first")
        self.assertEqual(artifact["manifest"]["records_file"], "records.jsonl")
        self.assertTrue(artifact["manifest"]["complete"])

        for index in range(3):
            clock.advance(1)
            recorder.record_phase(f"request-{index}", "request_error", outcome="error", status=502)
            with patch.object(recorder, "_ensure_control_thread_locked"):
                recorder.mark_incident("upstream_failure")
            clock.advance(1)
            recorder.process_due_incidents()
        self.assertEqual(recorder.status().incident_count, 3)
        self.assertGreaterEqual(recorder.status().incident_evicted_count, 1)
        self.assertIsNone(recorder.read_incident("i000001"))

    def test_faults_and_partial_tails_are_contained_and_recover(self) -> None:
        clock = FakeClock()
        root = self._root()
        rolling = root / "diagnostics" / "rolling"
        rolling.mkdir(parents=True)
        segment = rolling / "segment-00000001-1700000000000.jsonl"
        segment.write_text(
            '{"schema_version":1,"seq":1,"at_ms":1700000000000,"kind":"request_complete"}\n{"partial":',
            encoding="utf-8",
        )
        recorder = self._recorder(root, clock)
        recorder.record_phase("request-a", "request_complete", outcome="ok", status=200)
        self.assertTrue(recorder.flush(3))
        self.assertNotIn("partial", segment.read_text(encoding="utf-8"))

        with patch.object(recorder._sink, "append", side_effect=PermissionError("denied")):
            recorder.record_phase("request-b", "request_error", outcome="error", status=502)
            self.assertFalse(recorder.flush(3))
        recorder.record_phase("request-c", "request_complete", outcome="ok", status=200)
        self.assertTrue(recorder.flush(3))
        self.assertGreaterEqual(recorder.status().writer_failure_count, 1)

    def test_automatic_markers_and_time_retention_stay_bounded(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._recorder(
            root,
            clock,
            incident_tail_seconds=1,
            incident_retention_seconds=10,
        )
        recorder.observe_upstream_attempt(
            "first-request",
            attempt=1,
            retry_budget=1,
            elapsed_ms=61_000,
            outcome="error",
            failure_phase="headers",
        )
        self.assertEqual(recorder.status().last_marker_category, "abnormal_terminal")
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 1)
        self.assertIsNotNone(recorder.read_incident("i000001"))

        clock.advance(11)
        recorder.observe_close(
            "second-request",
            side="downstream",
            outcome="error",
            status=499,
            automatic_marker=True,
        )
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 1)
        self.assertIsNone(recorder.read_incident("i000001"))
        self.assertIsNotNone(recorder.read_incident("i000002"))
        self.assertEqual(recorder.status().incident_count, 1)

    def test_pending_incidents_are_bounded_before_freeze(self) -> None:
        clock = FakeClock()
        recorder = self._recorder(self._root(), clock, incident_tail_seconds=300, max_incidents=3)

        incident_ids = [recorder.mark_incident("manual") for _ in range(4)]

        self.assertEqual(incident_ids, ["i000001", "i000002", "i000003", None])
        self.assertTrue(recorder.status().truncated)

    def test_recovered_unknown_fields_do_not_reenter_a_frozen_artifact(self) -> None:
        clock = FakeClock()
        root = self._root()
        rolling = root / "diagnostics" / "rolling"
        rolling.mkdir(parents=True)
        (rolling / "segment-00000001-1700000000000.jsonl").write_text(
            '{"schema_version":1,"seq":1,"at_ms":1700000000000,"kind":"request_complete",'
            '"request":"r000001","prompt":"forbidden-recovered-prompt"}\n',
            encoding="utf-8",
        )
        # This recovery/privacy assertion drives the freeze synchronously so a
        # daemon wake-up cannot race the fake clock used by the fixture.
        with patch.object(diagnostic_recorder.DiagnosticRecorder, "_ensure_control_thread_locked"):
            recorder = self._recorder(root, clock, incident_tail_seconds=1)
            rolling_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (root / "diagnostics" / "rolling").glob("*.jsonl")
            )
            self.assertNotIn("forbidden-recovered-prompt", rolling_text)
            recorder.mark_incident("manual")
            clock.advance(1)
            self.assertEqual(recorder.process_due_incidents(), 1)
        artifact = recorder.read_incident("i000001")
        self.assertIsNotNone(artifact)
        assert artifact is not None
        self.assertNotIn("forbidden-recovered-prompt", json.dumps(artifact, ensure_ascii=True))
        self.assertTrue(all("prompt" not in record for record in artifact["records"]))

    def test_marker_preserves_available_prehistory_before_tail_rotation(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._deterministic_recorder(
            root,
            clock,
            rolling_window_seconds=10,
            incident_tail_seconds=5,
            max_segment_bytes=512,
            segment_seconds=1,
        )
        recorder.record_phase("before-marker", "downstream_accept", status=200, outcome="ok")
        self.assertTrue(recorder.flush(3))

        clock.advance(8)
        incident_id = recorder.mark_incident("manual")
        self.assertEqual(incident_id, "i000001")
        # Draining the marker is the writer-thread fence that captures the
        # marker-time rolling window without doing filesystem work on mark().
        self.assertTrue(recorder.flush(3))

        clock.advance(5)
        recorder.record_phase("tail", "request_complete", status=200, outcome="ok")
        self.assertEqual(recorder.process_due_incidents(), 1)

        artifact = recorder.read_incident(incident_id)
        self.assertIsNotNone(artifact)
        assert artifact is not None
        kinds = [record["kind"] for record in artifact["records"]]
        self.assertIn("downstream_accept", kinds)
        self.assertIn("incident_marker", kinds)
        self.assertIn("request_complete", kinds)

    def test_manifest_extra_forbidden_field_fails_closed_on_readback(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._deterministic_recorder(root, clock, incident_tail_seconds=1)
        recorder.record_phase("request", "request_complete", status=200, outcome="ok")
        incident_id = recorder.mark_incident("manual")
        assert incident_id is not None
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 1)

        manifest_path = root / "diagnostics" / "incidents" / f"incident-{incident_id}" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["prompt"] = "seeded-forbidden-manifest-content"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        self.assertIsNone(recorder.read_incident(incident_id))
        manifest.pop("prompt")
        manifest["incident_id"] = "i999999"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIsNone(recorder.read_incident(incident_id))

    def test_artifact_manifest_exposes_only_safe_build_identity(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._deterministic_recorder(
            root,
            clock,
            incident_tail_seconds=1,
            build_version="1.2.3-debug.4",
            source_revision="deadbeef",
        )
        recorder.record_phase("request", "request_complete", status=200, outcome="ok")
        incident_id = recorder.mark_incident("manual")
        assert incident_id is not None
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 1)

        artifact = recorder.read_incident(incident_id)
        self.assertIsNotNone(artifact)
        assert artifact is not None
        manifest = artifact["manifest"]
        self.assertEqual(manifest["build_version"], "1.2.3-debug.4")
        self.assertEqual(manifest["build_flavor"], "debug")
        self.assertEqual(manifest["source_revision"], "deadbeef")
        self.assertEqual(set(manifest), diagnostic_recorder._MANIFEST_FIELDS)

    def test_gateway_observation_schema_keeps_request_size_and_downstream_phases_content_free(self) -> None:
        clock = FakeClock()
        recorder = self._recorder(self._root(), clock)
        raw_request = "private-downstream-request"
        recorder.observe_proxy_event(
            "request_start",
            {
                "request_id": raw_request,
                "content_length": 4_096,
                "upstream": "official",
                "model": "openai/gpt-5.6",
                "route_mode": "official",
                "prompt": "forbidden content",
            },
        )
        recorder.observe_proxy_event(
            "downstream_response_open",
            {"request_id": raw_request, "status": 200},
        )
        recorder.observe_proxy_event("downstream_headers", {"request_id": raw_request, "status": 200})
        recorder.observe_proxy_event(
            "request_complete",
            {"request_id": raw_request, "status": 200, "duration_ms": 12},
        )
        self.assertTrue(recorder.flush(3))

        records = self._rolling_records(recorder.root.parent)
        self.assertEqual(
            [record["kind"] for record in records],
            [
                "downstream_accept",
                "downstream_body",
                "downstream_response_open",
                "downstream_headers",
                "request_complete",
                "downstream_write",
            ],
        )
        self.assertEqual(records[0]["request_size_bucket"], "1k-64k")
        self.assertEqual(records[1]["request_size_bucket"], "1k-64k")
        self.assertNotIn(raw_request, json.dumps(records, ensure_ascii=True))
        self.assertNotIn("forbidden content", json.dumps(records, ensure_ascii=True))

    def test_record_boundary_remains_strict_across_segment_overlap_and_clock_rollback(self) -> None:
        clock = FakeClock()
        root = self._root()
        recorder = self._recorder(
            root,
            clock,
            rolling_window_seconds=10,
            max_segment_bytes=512,
            segment_seconds=60,
        )
        recorder.record_phase("old", "request_complete", status=200, outcome="ok")
        clock.advance(9)
        recorder.record_phase("near-boundary", "request_complete", status=200, outcome="ok")
        clock.advance(2)
        recorder.record_phase("current", "request_complete", status=200, outcome="ok")
        self.assertTrue(recorder.flush(3))

        records = self._rolling_records(root)
        self.assertEqual([record["request"] for record in records], ["r000002", "r000003"])
        cutoff_before_rollback = max(record["at_ms"] for record in records) - 10_000
        self.assertTrue(all(record["at_ms"] >= cutoff_before_rollback for record in records))

        clock.value -= 120
        recorder.record_phase("after-rollback", "request_complete", status=200, outcome="ok")
        self.assertTrue(recorder.flush(3))
        records = self._rolling_records(root)
        self.assertNotIn("r000001", [record.get("request") for record in records])
        self.assertEqual([record["seq"] for record in records], sorted(record["seq"] for record in records))
        self.assertTrue(all(record["at_ms"] >= cutoff_before_rollback for record in records))

    def test_concurrent_markers_freeze_distinct_bounded_incidents(self) -> None:
        clock = FakeClock()
        recorder = self._deterministic_recorder(self._root(), clock, incident_tail_seconds=1, max_incidents=3)
        barrier = threading.Barrier(5)
        incident_ids: list[str | None] = []
        result_lock = threading.Lock()

        def mark() -> None:
            barrier.wait()
            incident_id = recorder.mark_incident("manual")
            with result_lock:
                incident_ids.append(incident_id)

        threads = [threading.Thread(target=mark) for _ in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

        accepted = sorted(incident_id for incident_id in incident_ids if incident_id is not None)
        self.assertEqual(accepted, ["i000001", "i000002", "i000003"])
        self.assertEqual(incident_ids.count(None), 1)
        self.assertTrue(recorder.flush(3))
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 3)
        self.assertEqual(recorder.status().incident_count, 3)

    def test_healthy_long_stream_frozen_artifact_replays_without_terminal_failure(self) -> None:
        clock = FakeClock()
        recorder = self._deterministic_recorder(self._root(), clock, incident_tail_seconds=1)
        raw_request = "healthy-stream-private-request"
        recorder.record_phase(raw_request, "downstream_accept", status=200, outcome="ok")
        for _ in range(50_000):
            recorder.observe_sse_line(raw_request, 17)
        recorder.observe_terminal(raw_request, forwarded=False)
        recorder.observe_terminal(raw_request, forwarded=True)
        incident_id = recorder.mark_incident("manual")
        assert incident_id is not None
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 1)

        artifact = recorder.read_incident(incident_id)
        self.assertIsNotNone(artifact)
        assert artifact is not None
        records = artifact["records"]
        self.assertEqual(artifact["manifest"]["classification"], "unknown")
        self.assertEqual(sum(record["kind"] == "sse_first" for record in records), 1)
        self.assertLessEqual(sum(record["kind"] == "sse_checkpoint" for record in records), 16)
        self.assertIn("upstream_terminal", [record["kind"] for record in records])
        self.assertIn("downstream_terminal", [record["kind"] for record in records])
        self.assertNotIn(raw_request, json.dumps(artifact, ensure_ascii=True))

    def test_restart_ordering_pause_resume_and_delete_are_deterministic(self) -> None:
        clock = FakeClock()
        root = self._root()
        first = diagnostic_recorder.DiagnosticRecorder(root, clock=clock, incident_tail_seconds=1)
        first.record_phase("first-request", "request_complete", outcome="ok", status=200)
        self.assertTrue(first.flush(3))
        self.assertTrue(first.shutdown(3))

        recorder = self._recorder(root, clock, incident_tail_seconds=1)
        paused = recorder.pause()
        self.assertTrue(paused.paused)
        recorder.record_phase("dropped-while-paused", "request_complete", outcome="ok", status=200)
        self.assertTrue(recorder.resume().active)
        recorder.record_phase("second-request", "downstream_close", outcome="error", status=499)
        with patch.object(recorder, "_ensure_control_thread_locked"):
            incident_id = recorder.mark_incident("manual")
        self.assertEqual(incident_id, "i000001")
        clock.advance(1)
        self.assertEqual(recorder.process_due_incidents(), 1)
        self.assertTrue(recorder.delete_incident(incident_id))
        self.assertFalse(recorder.delete_incident(incident_id))
        self.assertEqual(recorder.status().incident_count, 0)
        self.assertTrue(recorder.flush(3))

        records = self._rolling_records(root)
        self.assertEqual([record["request"] for record in records if "request" in record], ["r000001", "r000002"])
        self.assertEqual([record["seq"] for record in records], sorted(record["seq"] for record in records))

    def test_replay_classifier_distinguishes_all_first_close_shapes(self) -> None:
        def records(*kinds: str) -> list[dict[str, object]]:
            return [
                {"schema_version": 1, "seq": index, "at_ms": index, "kind": kind}
                for index, kind in enumerate(kinds, start=1)
            ]

        self.assertEqual(
            diagnostic_recorder.classify_frozen_records(records("downstream_close", "upstream_close")),
            "downstream-first",
        )
        self.assertEqual(
            diagnostic_recorder.classify_frozen_records(records("upstream_close", "downstream_close")),
            "upstream-first",
        )
        self.assertEqual(
            diagnostic_recorder.classify_frozen_records(records("upstream_terminal", "downstream_close")),
            "terminal-not-forwarded",
        )
        self.assertEqual(diagnostic_recorder.classify_frozen_records(records("request_complete")), "unknown")
