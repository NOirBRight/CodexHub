from __future__ import annotations

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

    def test_manual_launch_waits_for_and_reports_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(capture.tempfile, "gettempdir", return_value=temporary_directory), patch.object(
                capture,
                "_require_desktop_seams",
                return_value=(Path("desktop-package"), "test-build"),
            ):
                prepared = capture.prepare("gpt-5.6-terra")
                readiness = {"status": "ready", "readiness_indicator": "main_window", "exit_code": None}
                with patch.object(capture, "_launch_desktop", return_value=readiness) as launch_desktop:
                    launched = capture.launch(prepared["session"], "direct_official")

                self.assertEqual(launched["startup_readiness"], readiness)
                self.assertEqual(launched["launcher_strategy"], "explicit_user_data_arg")
                self.assertEqual(launch_desktop.call_args.args[-1], "explicit_user_data_arg")
                shutil.rmtree(capture._session_root(prepared["session"]))

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

    def test_gateway_summary_uses_opaque_request_labels_and_finds_upstream_first(self) -> None:
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
        self.assertEqual(summary["first_closing_side"], "gateway_or_upstream")
        self.assertEqual(summary["terminal_by_request"], {"request-1": "request_error"})
        self.assertEqual(summary["silent_terminal_request_count"], 0)


if __name__ == "__main__":
    unittest.main()
