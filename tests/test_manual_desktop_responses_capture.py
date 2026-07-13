from __future__ import annotations

import io
import tempfile
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import Mock, patch


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import manual_desktop_responses_capture as capture


class ManualDesktopResponsesCaptureTests(unittest.TestCase):
    def _prepare_isolated_session(self, temporary_directory: str) -> str:
        with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
            capture,
            "_require_desktop_seams",
            return_value=(Path("desktop-package"), "test-build"),
        ):
            return str(capture.prepare("gpt-5.6-terra")["session"])

    def _activate_direct_long_stream(self, session: str) -> None:
        capture.arm_long_stream(session)
        root = capture._session_root(session)
        state = capture._load_state(session)
        state["current_leg"] = "direct_official"
        capture._write_state(root, state)

    def test_launcher_command_changes_one_isolation_variable_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = Path("desktop-package") / "app" / "ChatGPT.exe"

            baseline = capture._desktop_command(executable, root, "baseline")
            explicit_user_data = capture._desktop_command(executable, root, "explicit_user_data_arg")
            disable_gpu = capture._desktop_command(executable, root, "disable_gpu")

            self.assertEqual(baseline, [str(executable)])
            self.assertEqual(explicit_user_data[:-1], baseline)
            self.assertTrue(explicit_user_data[-1].startswith("--user-data-dir="))
            self.assertEqual(disable_gpu, [str(executable), "--disable-gpu"])

    def test_launcher_diagnostic_summary_never_returns_raw_output(self) -> None:
        secret_diagnostic = b"Access is denied: private launcher detail"
        sink = {"bytes": len(secret_diagnostic), "prefix": bytearray(secret_diagnostic)}

        summary = capture._launcher_diagnostic_summary(sink)

        self.assertEqual(summary, {"bytes": len(secret_diagnostic), "category": "permission_denied"})
        self.assertNotIn("private launcher detail", str(summary))
        self.assertEqual(sink["prefix"], bytearray())

    def test_launch_readiness_recommends_only_a_verified_strategy(self) -> None:
        def probe(strategy: str, _duration: float) -> dict[str, object]:
            return {"strategy": strategy, "status": "ready" if strategy == "explicit_user_data_arg" else "not_ready"}

        with patch.object(capture, "_launch_readiness_probe", side_effect=probe):
            result = capture.launch_readiness("all", 3.0)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["recommended_launcher_strategy"], "explicit_user_data_arg")
        self.assertEqual(result["probe_count"], len(capture.LAUNCH_READINESS_STRATEGIES))

    def test_manual_bootstrap_launch_waits_for_and_reports_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
                capture,
                "_require_desktop_seams",
                return_value=(Path("desktop-package"), "test-build"),
            ):
                prepared = capture.prepare("gpt-5.6-terra")
                readiness = {"status": "ready", "readiness_indicator": "main_window", "exit_code": None}
                with patch.object(capture, "_launch_desktop", return_value=readiness) as launch_desktop:
                    launched = capture.launch(prepared["session"], capture.AUTH_BOOTSTRAP_LEG)

                self.assertEqual(launched["startup_readiness"], readiness)
                self.assertEqual(launched["launcher_strategy"], "explicit_user_data_arg")
                self.assertEqual(launched["route"], "disposable_auth_bootstrap")
                self.assertEqual(launch_desktop.call_args.args[-1], "explicit_user_data_arg")
                shutil.rmtree(capture._session_root(prepared["session"]))

    def test_direct_official_launch_is_cancelled_before_desktop_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
                capture,
                "_require_desktop_seams",
                return_value=(Path("desktop-package"), "test-build"),
            ):
                prepared = capture.prepare("gpt-5.6-terra")
                with patch.object(capture, "_launch_desktop") as launch_desktop:
                    with self.assertRaisesRegex(capture.CaptureError, "direct_official_control_cancelled"):
                        capture.launch(prepared["session"], "direct_official")

                launch_desktop.assert_not_called()
                shutil.rmtree(capture._session_root(prepared["session"]))

    def test_auth_bootstrap_is_not_a_direct_control_or_renderer_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
                capture,
                "_require_desktop_seams",
                return_value=(Path("desktop-package"), "test-build"),
            ):
                prepared = capture.prepare("gpt-5.6-terra")
                readiness = {"status": "ready", "readiness_indicator": "main_window", "exit_code": None}
                with patch.object(capture, "_launch_desktop", return_value=readiness), patch.object(
                    capture, "_start_gateway"
                ) as start_gateway:
                    launched = capture.launch(prepared["session"], capture.AUTH_BOOTSTRAP_LEG)

                self.assertEqual(launched["route"], "disposable_auth_bootstrap")
                self.assertEqual(launched["test_gateway"], "disconnected")
                start_gateway.assert_not_called()
                root = capture._session_root(prepared["session"])
                state = capture._load_state(prepared["session"])
                state["current_leg"] = capture.AUTH_BOOTSTRAP_LEG
                capture._write_state(root, state)
                with self.assertRaisesRegex(capture.CaptureError, "auth_bootstrap_has_no_renderer_result"):
                    capture.mark(prepared["session"], "completed")
                shutil.rmtree(root)

    def test_readiness_wait_rejects_a_tree_that_exits_before_window(self) -> None:
        process = Mock(pid=123)
        process.poll.return_value = 0
        snapshot = {
            "root_observed": False,
            "tree_alive": False,
            "main_window_seen": False,
            "app_server_seen": False,
            "desktop_child_count": 0,
            "helper_count": 0,
        }

        with patch.object(capture, "_desktop_process_snapshot", return_value=snapshot):
            readiness = capture._await_desktop_launch_readiness(process, 3.0)

        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(readiness["classification"], "process_tree_ended_before_readiness")

    def test_collect_records_windowless_owned_background_without_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
                capture,
                "_require_desktop_seams",
                return_value=(Path("desktop-package"), "test-build"),
            ):
                prepared = capture.prepare("gpt-5.6-terra")
                session = prepared["session"]
                root = capture._session_root(session)
                state = capture._load_state(session)
                state["current_app_pid"] = 123
                state["app_results"] = [{"leg": "direct_official", "result": "completed"}]
                capture._write_state(root, state)
                close_state = {
                    "state": "background_after_normal_close",
                    "identity_verified": True,
                    "visible_main_window": False,
                    "responsive": True,
                }
                with patch.object(capture, "_pid_alive", return_value=True), patch.object(
                    capture,
                    "_classify_isolated_background_desktop",
                    return_value=close_state,
                ):
                    report = capture.collect(session)

                self.assertEqual(report["status"], "collected_with_background_process")
                self.assertEqual(report["desktop_process"], close_state)
                self.assertEqual(report["cleanup"]["desktop_process_teardown"], "not_permitted")
                self.assertFalse(report["session_reusable"])
                shutil.rmtree(root)

    def test_collect_refuses_visible_or_unverified_background_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
                capture,
                "_require_desktop_seams",
                return_value=(Path("desktop-package"), "test-build"),
            ):
                prepared = capture.prepare("gpt-5.6-terra")
                session = prepared["session"]
                root = capture._session_root(session)
                state = capture._load_state(session)
                state["current_app_pid"] = 123
                capture._write_state(root, state)
                with patch.object(capture, "_pid_alive", return_value=True), patch.object(
                    capture,
                    "_classify_isolated_background_desktop",
                    return_value={"state": "visible_window_still_open"},
                ):
                    with self.assertRaisesRegex(capture.CaptureError, "close_isolated_desktop_before_collection"):
                        capture.collect(session)

                shutil.rmtree(root)

    def test_long_stream_markers_report_a_sustained_completed_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory):
                session = self._prepare_isolated_session(temporary_directory)
                root = capture._session_root(session)
                self._activate_direct_long_stream(session)
                timestamps = [
                    "2026-07-13T00:00:00Z",
                    "2026-07-13T00:00:45Z",
                    "2026-07-13T00:01:00Z",
                ]
                with patch.object(capture, "_utc_now", side_effect=timestamps):
                    capture.mark_long_stream(session, "first_visible_output")
                    capture.mark_long_stream(session, "stream_active_target_reached")
                    capture.mark(session, "completed")

                report = capture.collect(session)

                self.assertEqual(report["status"], "collected")
                self.assertEqual(report["long_stream"]["capture_status"], "complete")
                self.assertEqual(report["long_stream"]["qualification"], "sustained_control_observed")
                self.assertEqual(report["long_stream"]["first_visible_to_target_ms"], 45000)
                self.assertEqual(report["long_stream"]["first_visible_to_terminal_ms"], 60000)
                self.assertEqual(report["long_stream"]["terminal_count"], 1)
                self.assertNotIn("prompt_content", report["long_stream"])
                shutil.rmtree(root)

    def test_long_stream_rejects_target_without_first_visible_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory):
                session = self._prepare_isolated_session(temporary_directory)
                root = capture._session_root(session)
                self._activate_direct_long_stream(session)

                with self.assertRaisesRegex(capture.CaptureError, "long_stream_first_visible_output_required"):
                    capture.mark_long_stream(session, "stream_active_target_reached")

                state = capture._load_state(session)
                self.assertEqual(state["long_stream"]["phase"], "awaiting_first_visible_output")
                shutil.rmtree(root)

    def test_long_stream_rejects_duplicate_or_out_of_order_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory):
                session = self._prepare_isolated_session(temporary_directory)
                root = capture._session_root(session)
                self._activate_direct_long_stream(session)
                capture.mark_long_stream(session, "first_visible_output")

                with self.assertRaisesRegex(capture.CaptureError, "long_stream_marker_out_of_order"):
                    capture.mark_long_stream(session, "first_visible_output")

                capture.mark_long_stream(session, "stream_active_target_reached")
                capture.mark(session, "completed")
                with self.assertRaisesRegex(capture.CaptureError, "duplicate_long_stream_terminal"):
                    capture.mark(session, "stream_disconnected")

                shutil.rmtree(root)

    def test_long_stream_missing_target_or_terminal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory):
                session = self._prepare_isolated_session(temporary_directory)
                root = capture._session_root(session)
                self._activate_direct_long_stream(session)
                capture.mark_long_stream(session, "first_visible_output")

                missing_target_and_terminal = capture.collect(session)

                self.assertEqual(missing_target_and_terminal["status"], "incomplete_long_stream_capture")
                self.assertEqual(
                    missing_target_and_terminal["long_stream"]["qualification"],
                    "target_and_terminal_missing",
                )
                shutil.rmtree(root)

    def test_long_stream_missing_terminal_fails_closed_after_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory):
                session = self._prepare_isolated_session(temporary_directory)
                root = capture._session_root(session)
                self._activate_direct_long_stream(session)
                with patch.object(
                    capture,
                    "_utc_now",
                    side_effect=["2026-07-13T00:00:00Z", "2026-07-13T00:00:45Z"],
                ):
                    capture.mark_long_stream(session, "first_visible_output")
                    capture.mark_long_stream(session, "stream_active_target_reached")

                report = capture.collect(session)

                self.assertEqual(report["status"], "incomplete_long_stream_capture")
                self.assertEqual(report["long_stream"]["qualification"], "terminal_missing")
                shutil.rmtree(root)

    def test_long_stream_refuses_a_gateway_leg_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory):
                session = self._prepare_isolated_session(temporary_directory)
                root = capture._session_root(session)
                capture.arm_long_stream(session)

                with self.assertRaisesRegex(capture.CaptureError, "long_stream_requires_direct_official"):
                    capture.launch(session, "gateway_official_auto")

                shutil.rmtree(root)

    def test_long_stream_early_disconnect_is_retained_but_under_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory):
                session = self._prepare_isolated_session(temporary_directory)
                root = capture._session_root(session)
                self._activate_direct_long_stream(session)
                capture.mark_long_stream(session, "first_visible_output")
                capture.mark(session, "stream_disconnected")

                report = capture.collect(session)

                self.assertEqual(report["status"], "incomplete_long_stream_capture")
                self.assertEqual(report["long_stream"]["qualification"], "under_target_stream_disconnected")
                self.assertEqual(report["long_stream"]["terminal_count"], 1)
                shutil.rmtree(root)

    def test_long_stream_prompt_is_never_committed_as_capture_content(self) -> None:
        source = Path(capture.__file__).read_text(encoding="utf-8")

        self.assertIn(capture.LONG_STREAM_PROMPT_IDENTIFIER, source)
        self.assertIn(capture.LONG_STREAM_PROMPT_SHA256, source)
        self.assertNotIn("LONG_STREAM_PROMPT =", source)
        self.assertNotIn("prompt_content", source)

    def test_prepare_and_collect_keep_profile_disposable_and_credential_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
                capture,
                "_require_desktop_seams",
                return_value=(Path("desktop-package"), "test-build"),
            ):
                prepared = capture.prepare("gpt-5.6-terra")
                session = prepared["session"]
                root = capture._session_root(session)
                paths = capture._runtime_paths(root)
                self.assertFalse((paths["codex_home"] / "auth.json").exists())
                self.assertNotIn("experimental_bearer_token", paths["config"].read_text(encoding="utf-8"))

                report = capture.collect(session)

                self.assertEqual(report["status"], "collected")
                self.assertFalse(report["cleanup"]["shared_state_touched"])
                self.assertEqual(report["cleanup"]["gateway"], "not_started")
                shutil.rmtree(root)

    def test_gateway_summary_uses_opaque_request_labels_and_defers_first_close_to_boundary_trace(self) -> None:
        raw_request_id = "private-request-id"
        summary = capture._summarize_gateway_events(
            {"session_hmac_key": "a" * 64},
            [
                {"event": "request_start", "request_id": raw_request_id},
                {
                    "event": "upstream_retry",
                    "request_id": raw_request_id,
                    "failure_phase": "response_headers",
                    "error": "RemoteDisconnected",
                },
                {"event": "request_error", "request_id": raw_request_id, "status": 502},
            ],
        )

        rendered = str(summary)
        self.assertNotIn(raw_request_id, rendered)
        self.assertEqual(summary["first_closing_side"], "unknown")
        self.assertEqual(summary["classification_source"], "boundary_trace_required")
        self.assertEqual(summary["terminal_by_request"], {"request-1": "request_error"})
        self.assertEqual(summary["silent_terminal_request_count"], 0)

    def test_boundary_trace_uses_live_stream_order_not_exception_classification(self) -> None:
        raw_request_id = "private-request-id"
        summary = capture._summarize_gateway_boundary_trace(
            [
                {
                    "sequence": 1,
                    "elapsed_ms": 0,
                    "event": "downstream_body_read_complete",
                    "request": "request-1",
                    "downstream_connection": "connection-1",
                },
                {
                    "sequence": 2,
                    "elapsed_ms": 3,
                    "event": "upstream_response_headers",
                    "request": "request-1",
                    "protocol": "official",
                    "status_class": 2,
                },
                {
                    "sequence": 3,
                    "elapsed_ms": 5,
                    "event": "upstream_sse_line",
                    "request": "request-1",
                    "bytes": 48,
                    "line_complete": True,
                    "sse_event_complete": False,
                },
                {
                    "sequence": 4,
                    "elapsed_ms": 7,
                    "event": "upstream_sse_line",
                    "request": "request-1",
                    "bytes": 1,
                    "line_complete": True,
                    "sse_event_complete": True,
                },
                {
                    "sequence": 5,
                    "elapsed_ms": 9,
                    "event": "upstream_sse_read_failed",
                    "request": "request-1",
                    "error": "IncompleteRead",
                    "failure_phase": "sse_read",
                },
                {
                    "sequence": 6,
                    "elapsed_ms": 10,
                    "event": "downstream_write_after_upstream_failure_succeeded",
                    "request": "request-1",
                    "bytes": 32,
                },
            ]
        )

        rendered = str(summary)
        self.assertNotIn(raw_request_id, rendered)
        self.assertTrue(summary["trace_order_valid"])
        self.assertEqual(summary["first_closing_side"], "upstream_transport")
        self.assertEqual(summary["first_failure_phase"], "sse_read")
        self.assertEqual(summary["requests"]["request-1"]["upstream"]["first_sse_elapsed_ms"], 5)
        self.assertEqual(summary["requests"]["request-1"]["upstream"]["last_complete_sse_elapsed_ms"], 7)

    def test_boundary_trace_does_not_classify_exception_or_error_headers_without_close_evidence(self) -> None:
        upstream_reader_exception = capture._summarize_gateway_boundary_trace(
            [
                {"sequence": 1, "elapsed_ms": 0, "event": "downstream_body_read_complete", "request": "request-1"},
                {"sequence": 2, "elapsed_ms": 3, "event": "upstream_response_headers", "request": "request-1"},
                {
                    "sequence": 3,
                    "elapsed_ms": 8,
                    "event": "upstream_sse_read_failed",
                    "request": "request-1",
                    "failure_phase": "sse_read",
                    "error": "IncompleteRead",
                },
            ]
        )
        service_error_headers = capture._summarize_gateway_boundary_trace(
            [
                {"sequence": 1, "elapsed_ms": 0, "event": "downstream_body_read_complete", "request": "request-1"},
                {
                    "sequence": 2,
                    "elapsed_ms": 3,
                    "event": "upstream_service_error_headers",
                    "request": "request-1",
                    "status_class": 5,
                },
            ]
        )

        self.assertEqual(upstream_reader_exception["first_closing_side"], "unknown")
        self.assertEqual(upstream_reader_exception["first_failure_phase"], "unknown")
        self.assertEqual(service_error_headers["first_closing_side"], "unknown")
        self.assertEqual(service_error_headers["first_failure_phase"], "unknown")

    def test_boundary_writer_excludes_headers_from_downstream_body_exposure(self) -> None:
        class _Stats:
            terminal_event_seen = False
            completed_event_seen = False

            def observe_line(self, _data: bytes) -> None:
                return None

        request_state = {
            "request": "request-1",
            "downstream_connection": "connection-1",
            "upstream_sse_stats": _Stats(),
            "downstream_sse_stats": _Stats(),
            "downstream_headers_complete": False,
            "downstream_bytes_exposed": 0,
            "downstream_lines_exposed": 0,
            "downstream_first_exposed": False,
            "downstream_terminal_forwarded": False,
            "downstream_write_failed": False,
        }
        events: list[dict[str, object]] = []
        writer = capture._BoundaryTraceWriter(
            io.BytesIO(),
            request_state=request_state,
            record=events.append,
        )

        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n\r\n")
        self.assertEqual(request_state["downstream_bytes_exposed"], 0)
        self.assertFalse(request_state["downstream_first_exposed"])

        request_state["downstream_headers_complete"] = True
        writer.write(b"data: {}\n\n")

        self.assertEqual(request_state["downstream_bytes_exposed"], len(b"data: {}\n\n"))
        self.assertTrue(request_state["downstream_first_exposed"])
        self.assertEqual([event["event"] for event in events], ["downstream_first_exposed"])

    def test_boundary_writer_confirms_downstream_writability_after_upstream_failure(self) -> None:
        class _Stats:
            terminal_event_seen = False
            completed_event_seen = False

            def observe_line(self, _data: bytes) -> None:
                return None

        request_state = {
            "request": "request-1",
            "downstream_connection": "connection-1",
            "upstream_sse_stats": _Stats(),
            "downstream_sse_stats": _Stats(),
            "downstream_headers_complete": False,
            "downstream_bytes_exposed": 0,
            "downstream_lines_exposed": 0,
            "downstream_first_exposed": False,
            "downstream_terminal_forwarded": False,
            "downstream_write_failed": False,
            "upstream_failure_observed": True,
            "downstream_post_upstream_failure_write_confirmed": False,
        }
        events: list[dict[str, object]] = []
        writer = capture._BoundaryTraceWriter(
            io.BytesIO(),
            request_state=request_state,
            record=events.append,
        )

        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n")

        self.assertTrue(request_state["downstream_post_upstream_failure_write_confirmed"])
        self.assertEqual(
            [event["event"] for event in events],
            ["downstream_write_after_upstream_failure_succeeded"],
        )

    def test_boundary_trace_marks_downstream_write_before_later_upstream_fault(self) -> None:
        summary = capture._summarize_gateway_boundary_trace(
            [
                {
                    "sequence": 1,
                    "elapsed_ms": 0,
                    "event": "downstream_response_open",
                    "request": "request-1",
                    "status_class": 2,
                },
                {
                    "sequence": 2,
                    "elapsed_ms": 8,
                    "event": "downstream_write_failed",
                    "request": "request-1",
                    "failure_phase": "downstream_write",
                    "error": "ConnectionResetError",
                },
                {
                    "sequence": 3,
                    "elapsed_ms": 11,
                    "event": "upstream_sse_read_failed",
                    "request": "request-1",
                    "failure_phase": "sse_read",
                    "error": "IncompleteRead",
                },
            ]
        )

        self.assertEqual(summary["first_closing_side"], "downstream_client")
        self.assertEqual(summary["first_failure_phase"], "downstream_write")

    def test_boundary_trace_fails_closed_on_invalid_order_or_exception_only(self) -> None:
        invalid_order = capture._summarize_gateway_boundary_trace(
            [
                {"sequence": 2, "elapsed_ms": 4, "event": "upstream_open_failed", "request": "request-1"},
                {"sequence": 1, "elapsed_ms": 5, "event": "gateway_event", "request": "request-1"},
            ]
        )
        exception_only = capture._summarize_gateway_boundary_trace(
            [
                {
                    "sequence": 1,
                    "elapsed_ms": 0,
                    "event": "gateway_event",
                    "request": "request-1",
                    "gateway_event": "upstream_retry",
                    "error": "IncompleteRead",
                }
            ]
        )

        self.assertFalse(invalid_order["trace_order_valid"])
        self.assertEqual(invalid_order["first_closing_side"], "unknown")
        self.assertEqual(invalid_order["first_failure_phase"], "unknown")
        self.assertEqual(exception_only["first_closing_side"], "unknown")
        self.assertEqual(exception_only["first_failure_phase"], "unknown")

    def test_boundary_trace_records_desktop_gateway_boundary_and_route_mode(self) -> None:
        summary = capture._summarize_gateway_boundary_trace(
            [
                {
                    "sequence": 1,
                    "elapsed_ms": 0,
                    "event": "downstream_request_bound",
                    "request": "request-1",
                    "downstream_connection": "connection-1",
                    "desktop_route_mode": "isolated_gateway_loopback",
                    "app_stream_consumer_boundary": "gateway_downstream_socket",
                },
                {
                    "sequence": 2,
                    "elapsed_ms": 2,
                    "event": "upstream_attempt_begin",
                    "request": "request-1",
                    "protocol": "official",
                    "effective_proxy_mode": "auto_direct",
                    "connection_disposition": "unobserved",
                },
            ]
        )

        request = summary["requests"]["request-1"]
        self.assertEqual(request["route"]["desktop_route_mode"], "isolated_gateway_loopback")
        self.assertEqual(request["downstream"]["stream_consumer_boundary"], "gateway_downstream_socket")
        self.assertTrue(request["downstream"]["request_observed"])

    def test_response_connection_identity_requires_an_observed_connection_handle(self) -> None:
        class _Socket:
            pass

        class _Connection:
            def __init__(self, socket_value: object | None) -> None:
                self.sock = socket_value

        class _RawResponse:
            def __init__(self, connection: object | None) -> None:
                self.connection = connection

        class _Response:
            def __init__(self, raw_response: object) -> None:
                self._response = raw_response

        socket_value = _Socket()
        self.assertIs(
            capture._response_connection_identity(_Response(_RawResponse(_Connection(socket_value)))),
            socket_value,
        )

        class _RawStream:
            def __init__(self, nested_socket: object) -> None:
                self._sock = nested_socket

        class _FilePointer:
            def __init__(self, raw_stream: object) -> None:
                self.raw = raw_stream

        class _StdlibResponse:
            def __init__(self, file_pointer: object) -> None:
                self.fp = file_pointer

        self.assertIs(
            capture._response_connection_identity(_StdlibResponse(_FilePointer(_RawStream(socket_value)))),
            socket_value,
        )
        self.assertIsNone(capture._response_connection_identity(_Response(_RawResponse(None))))

    def test_clean_gateway_capture_retains_only_gateway_fault_readiness(self) -> None:
        readiness = capture._capture_readiness_after_collection(
            {"app_results": [{"leg": "gateway_official_auto", "result": "completed"}]},
            {"first_closing_side": "unknown"},
        )

        self.assertEqual(readiness["status"], "retained_for_next_natural_faulty_window")
        self.assertFalse(readiness["automatic_retest"])
        self.assertEqual(readiness["rearm"], "new_disposable_gateway_only_session")
        self.assertEqual(readiness["direct_official_control"], "cancelled")

    def test_current_transport_scope_reports_no_global_official_opener_installation(self) -> None:
        scope = capture._current_gateway_transport_scope()

        self.assertEqual(scope["official_transport"], "private_urllib3_pool")
        self.assertEqual(scope["external_transport"], "stdlib_urlopen")
        self.assertEqual(scope["official_global_opener_installation"], "absent_current_source")


if __name__ == "__main__":
    unittest.main()
