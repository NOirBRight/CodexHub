from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gateway_settings
from route_primitives import (
    RETRY_REQUEST_COMPACT,
    RETRY_REQUEST_IMAGE_PROXY_VISION,
    RETRY_REQUEST_MAIN_GENERATION,
)


class GatewaySettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_proxy_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_proxy_dir.cleanup)
        runtime_proxy_patch = patch(
            "gateway_settings._runtime_proxy_dir",
            return_value=Path(runtime_proxy_dir.name),
        )
        runtime_proxy_patch.start()
        self.addCleanup(runtime_proxy_patch.stop)

    def test_retry_attempts_are_bounded_by_request_kind(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "30",
                "CODEX_PROXY_COMPACT_RETRY_MAX_ATTEMPTS": "3",
                "CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS": "2",
            },
            clear=False,
        ):
            self.assertEqual(gateway_settings._upstream_retry_attempts(RETRY_REQUEST_COMPACT), 3)
            self.assertEqual(gateway_settings._upstream_retry_attempts(RETRY_REQUEST_MAIN_GENERATION), 2)
            self.assertEqual(gateway_settings._upstream_retry_attempts(RETRY_REQUEST_IMAGE_PROXY_VISION), 3)

    def test_default_retry_attempts_by_request_kind(self):
        with patch.dict(os.environ, {"CODEX_PROXY_AUTO_RETRY_ENABLED": "1"}, clear=False):
            self.assertEqual(gateway_settings._upstream_retry_attempts(RETRY_REQUEST_MAIN_GENERATION), 5)
            self.assertEqual(gateway_settings._upstream_retry_attempts(RETRY_REQUEST_COMPACT), 3)
            self.assertEqual(gateway_settings._upstream_retry_attempts(RETRY_REQUEST_IMAGE_PROXY_VISION), 3)
