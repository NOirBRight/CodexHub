from __future__ import annotations

import json
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
        self.runtime_proxy_dir = Path(runtime_proxy_dir.name)
        runtime_proxy_patch = patch(
            "gateway_settings._runtime_proxy_dir",
            return_value=self.runtime_proxy_dir,
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

    def test_gateway_auto_retry_settings_default_to_enabled_thirty_attempts(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(gateway_settings.gateway_auto_retry_enabled())
            self.assertEqual(gateway_settings.gateway_auto_retry_max_attempts(), 30)

    def test_official_http_passthrough_setting_defaults_enabled_and_env_can_disable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(gateway_settings.gateway_official_http_passthrough_enabled())
        with patch.dict(os.environ, {"CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED": "0"}, clear=True):
            self.assertFalse(gateway_settings.gateway_official_http_passthrough_enabled())

    def test_max_request_body_bytes_defaults_and_env_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gateway_settings.max_request_body_bytes(), 64 * 1024 * 1024)
        with patch.dict(os.environ, {"CODEX_PROXY_MAX_REQUEST_BODY_BYTES": "1024"}, clear=True):
            self.assertEqual(gateway_settings.max_request_body_bytes(), 1024)
        with patch.dict(os.environ, {"CODEX_PROXY_MAX_REQUEST_BODY_BYTES": "bad"}, clear=True):
            self.assertEqual(gateway_settings.max_request_body_bytes(), 64 * 1024 * 1024)

    def test_official_upstream_open_attempts_are_hard_capped_at_two(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gateway_settings.official_upstream_open_attempts(), 2)
        with patch.dict(
            os.environ,
            {"CODEX_PROXY_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS": "99"},
            clear=True,
        ):
            self.assertEqual(gateway_settings.official_upstream_open_attempts(), 2)
        with patch.dict(
            os.environ,
            {"CODEX_PROXY_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS": "1"},
            clear=True,
        ):
            self.assertEqual(gateway_settings.official_upstream_open_attempts(), 1)

    def test_gateway_auto_retry_settings_fall_back_to_runtime_settings_when_env_missing(self):
        (self.runtime_proxy_dir / "settings.json").write_text(
            json.dumps(
                {
                    "gateway_auto_retry_enabled": False,
                    "gateway_auto_retry_max_attempts": 4,
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(gateway_settings.gateway_auto_retry_enabled())
            self.assertEqual(gateway_settings.gateway_auto_retry_max_attempts(), 4)

    def test_gateway_timeout_settings_fall_back_to_runtime_settings_when_env_missing(self):
        (self.runtime_proxy_dir / "settings.json").write_text(
            json.dumps({"gateway_request_timeout_seconds": 45}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gateway_settings.upstream_timeout_seconds(), 45)

    def test_gateway_auto_retry_runtime_settings_override_stale_env(self):
        (self.runtime_proxy_dir / "settings.json").write_text(
            json.dumps(
                {
                    "gateway_auto_retry_enabled": False,
                    "gateway_auto_retry_max_attempts": 2,
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "30",
            },
            clear=False,
        ):
            self.assertFalse(gateway_settings.gateway_auto_retry_enabled())
            self.assertEqual(gateway_settings.gateway_auto_retry_max_attempts(), 2)

    def test_model_event_idle_timeout_uses_new_env_before_legacy_setting_alias(self):
        (self.runtime_proxy_dir / "settings.json").write_text(
            json.dumps({"gateway_post_content_sse_idle_timeout_seconds": 60}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gateway_settings.model_event_sse_idle_timeout_seconds(), 60)
        with patch.dict(os.environ, {"CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS": "300"}, clear=True):
            self.assertEqual(gateway_settings.model_event_sse_idle_timeout_seconds(), 300)
