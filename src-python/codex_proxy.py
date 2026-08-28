"""CodexHub Gateway process entry.

Owns the HTTP server wiring: `CodexProxyHandler` (routing + health),
`run_server`, and `main`. All request-handling behavior lives in
`gateway_handler_impl.GatewayHandlerMixin` and the owning modules.
"""

from __future__ import annotations

import sys

from python_runtime_contract import require_python_313

require_python_313(__file__)

# When this file is launched as a script, publish the running module under
# ``codex_proxy`` before sibling imports bind the process-wide event sink.
if __name__ == "__main__":
    sys.modules.setdefault("codex_proxy", sys.modules[__name__])

from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
VENDORED_URLLIB3_WHEEL = VENDOR_DIR / "urllib3-2.7.0-py3-none-any.whl"
if not VENDORED_URLLIB3_WHEEL.is_file():
    raise RuntimeError(f"missing pinned Gateway transport dependency: {VENDORED_URLLIB3_WHEEL}")
sys.path.insert(0, str(VENDORED_URLLIB3_WHEEL))

import argparse
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import gateway_admission
import gateway_catalog_runtime
import gateway_events
import gateway_settings
import route_plan as _route_plan_module
from gateway_admission import (
    USER_REQUESTED_SHUTDOWN_OUTCOME,
    gateway_shutdown_controller_for_handler as _gateway_shutdown_controller_for_handler,
)
from gateway_errors import local_gateway_auth_error_payload as _local_gateway_auth_error_payload
from gateway_events import GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS
from gateway_handler_impl import GatewayHandlerMixin
from gateway_request import (
    is_websocket_upgrade as _is_websocket_upgrade,
    local_request_authorized as _local_request_authorized,
    provider_scoped_path,
    request_context_from_headers,
)
from protocol_translation import PreparedExchange

logger = logging.getLogger("codex_proxy")

PROXY_TEXT_LOG_PATH = gateway_settings._runtime_proxy_dir() / "codex-proxy.log"

PROXY_BUILD = "2026-07-04-browser-tool-exposure"
PROXY_FEATURES = [
    "compressed-request-routing",
    "provider-alias-routing",
    "local-responses-probe-fast-reject",
    "internal-history-item-normalization",
    "external-reasoning-hidden",
    "tool-name-guard",
    "third-party-subagent-tool-alias",
    "third-party-tool-search-call-shim",
    "third-party-multi-agent-discovery-shim",
    "third-party-multi-agent-namespace-shim",
    "third-party-multi-agent-wait-close-argument-shim",
    "third-party-explicit-codex-native-tools",
    "third-party-json-schema-type-array-guard",
    "third-party-multi-agent-discovery-fallback",
    "third-party-native-tools-stay-visible",
    "third-party-multi-agent-discovery-guidance",
    "third-party-tool-search-disabled",
    "third-party-spawn-hidden-while-agent-open",
    "third-party-multi-agent-status-guidance",
    "third-party-unsupported-reasoning-strip",
    "third-party-subagent-observability",
    "official-invalid-tool-assistant-shim",
    "upstream-incomplete-read-guard",
    "chat-completions-gateway",
    "third-party-open-agent-id-schema-guidance",
    "third-party-ordered-agent-lifecycle-guidance",
    "third-party-single-loop-completion-gate",
    "ollama-output-token-cap",
    "official-upstream-open-retry",
    "compact-text-only-tool-strip",
    "compact-empty-response-guard",
    "compact-empty-response-retry",
    "stream-read-error-retry-before-downstream",
    "downstream-sse-keepalive",
    "split-transport-model-event-sse-idle-timeouts",
    "capacity-aware-upstream-retry",
    "stream-transient-global-retry-budget",
    "third-party-tool-terminal-synthesis",
    "browser-context-skill-guidance",
    "third-party-multi-agent-deterministic-repair",
    "third-party-required-subagent-action-repair",
    "third-party-chat-output-repair-parity",
    "official-upstream-connection-pool",
    "official-upstream-idle-connection-expiry",
    "official-terminal-sse-authoritative",
    "official-title-responses-lite-header-strip",
    "zstd-request-body-runtime",
    "raw-provider-probe-opt-out",
]


def _forward_planning_event(event: str, **fields: Any) -> None:
    gateway_events.write_proxy_event(event, **fields)


_route_plan_module._planning_event_sink = _forward_planning_event


class CodexProxyHandler(GatewayHandlerMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _active_prepared_exchange: PreparedExchange | None = None

    def handle_one_request(self) -> None:
        self._active_prepared_exchange: PreparedExchange | None = None
        self._diagnostic_request_id: str | None = None
        try:
            super().handle_one_request()
        finally:
            self._active_prepared_exchange = None
            self._diagnostic_request_id = None

    def _observe_downstream_phase(self, event: str, *, status: int | None = None) -> None:
        request_id = getattr(self, "_diagnostic_request_id", None)
        if not isinstance(request_id, str) or not request_id:
            return
        fields: dict[str, Any] = {"request_id": request_id}
        if isinstance(status, int):
            fields["status"] = status
        gateway_events.observe_gateway_diagnostic("observe_proxy_event", event, fields)

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        self._observe_downstream_phase("downstream_response_open", status=code)

    def end_headers(self) -> None:
        super().end_headers()
        self._observe_downstream_phase("downstream_headers")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if _is_websocket_upgrade(self.headers) and gateway_settings.gateway_websocket_recorder_enabled():
            self._handle_websocket_recording_probe()
            return
        if parsed.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "build": PROXY_BUILD,
                    "features": PROXY_FEATURES,
                },
            )
            return
        if parsed.path == "/v1/models":
            catalog = gateway_catalog_runtime.current_catalog_data()
            if gateway_catalog_runtime.wants_codex_model_manifest(parsed.query):
                self._send_json(200, gateway_catalog_runtime.codex_model_manifest(catalog))
            else:
                self._send_json(200, gateway_catalog_runtime.openai_model_list(catalog))
            return
        if parsed.path == "/v1/responses":
            if _is_websocket_upgrade(self.headers):
                self._reject_local_responses_websocket_probe()
                return
            self._send_local_responses_no_content()
            return
        if parsed.path.startswith("/v1/responses/"):
            self._passthrough_official_control_request("GET")
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/shutdown":
            request_context = request_context_from_headers(self.headers)
            if not _local_request_authorized(self.headers, request_context):
                self._send_json(401, _local_gateway_auth_error_payload())
                self.close_connection = True
                return
            controller = _gateway_shutdown_controller_for_handler(self)
            controller.close_admission()
            self._send_json(
                200,
                {
                    "ok": True,
                    "outcome": USER_REQUESTED_SHUTDOWN_OUTCOME,
                },
            )
            self.close_connection = True
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if parsed.path == "/v1/responses":
            self._proxy_post_request(inbound_format="responses")
            return
        provider_hint = provider_scoped_path(parsed.path, "responses")
        if provider_hint is not None:
            self._proxy_post_request(inbound_format="responses", provider_hint=provider_hint)
            return

        if parsed.path == "/v1/chat/completions":
            self._proxy_post_request(inbound_format="chat_completions")
            return
        provider_hint = provider_scoped_path(parsed.path, "chat/completions")
        if provider_hint is not None:
            self._proxy_post_request(inbound_format="chat_completions", provider_hint=provider_hint)
            return

        if parsed.path == "/v1/images/generations":
            self._proxy_official_image_generation()
            return

        self._send_json_and_close(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    @classmethod
    def unbound(cls) -> "CodexProxyHandler":
        """Construct a handler without binding it to a socket, for isolated tests."""
        return cls.__new__(cls)


def run_server(host: str, port: int) -> None:
    PROXY_TEXT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(PROXY_TEXT_LOG_PATH, encoding="utf-8"),
        ],
        force=True,
    )
    server = ThreadingHTTPServer((host, port), CodexProxyHandler)
    server.daemon_threads = True
    shutdown_controller = gateway_admission.GatewayShutdownController()
    server.gateway_shutdown_controller = shutdown_controller
    logger.info("serving Codex proxy on %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if shutdown_controller.shutdown_requested:
            shutdown_controller.wait_for_active_requests()
            flush_timeout = min(
                GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS,
                shutdown_controller.remaining_shutdown_budget_seconds(),
            )
        else:
            flush_timeout = GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS
        writer_result = gateway_events.GATEWAY_EVENT_WRITER.shutdown(
            timeout=flush_timeout,
        )
        if not writer_result.completed:
            logger.warning("Gateway event writer shutdown ended with %s", writer_result.outcome)
        try:
            diagnostic_shutdown = getattr(gateway_events.GATEWAY_DIAGNOSTIC_RECORDER, "shutdown", None)
            diagnostic_timeout = (
                min(
                    GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS,
                    shutdown_controller.remaining_shutdown_budget_seconds(),
                )
                if shutdown_controller.shutdown_requested
                else GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS
            )
            if callable(diagnostic_shutdown) and not diagnostic_shutdown(diagnostic_timeout):
                logger.warning("Gateway diagnostic recorder shutdown did not drain")
        except Exception:
            logger.warning("Gateway diagnostic recorder shutdown failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex model routing proxy.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
