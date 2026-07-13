from __future__ import annotations

import tempfile
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import manual_desktop_responses_capture as capture


class ManualDesktopResponsesCaptureTests(unittest.TestCase):
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
