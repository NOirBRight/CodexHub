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

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import logging
import threading
from typing import Any
from urllib.parse import urlsplit

# Execute helper definitions into this module dict so ``codex_proxy.<name>``
# patches stay live. ``gateway_runtime`` remains importable for source tests.
_RUNTIME_IMPL = Path(__file__).with_name("gateway_runtime.py")
exec(compile(_RUNTIME_IMPL.read_text(encoding="utf-8"), str(_RUNTIME_IMPL), "exec"), globals())
import route_plan as _route_plan_module


def _forward_planning_event(event: str, **fields: Any) -> None:
    write_proxy_event(event, **fields)


_route_plan_module._planning_event_sink = _forward_planning_event


class CodexProxyHandler(BaseHTTPRequestHandler):
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
        _observe_gateway_diagnostic("observe_proxy_event", event, fields)

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        self._observe_downstream_phase("downstream_response_open", status=code)

    def end_headers(self) -> None:
        super().end_headers()
        self._observe_downstream_phase("downstream_headers")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if _is_websocket_upgrade(self.headers) and gateway_websocket_recorder_enabled():
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
            self._send_json(200, openai_model_list(current_catalog_data()))
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


_HANDLER_IMPL = Path(__file__).with_name("gateway_handler_impl.py")
exec(compile(_HANDLER_IMPL.read_text(encoding="utf-8"), str(_HANDLER_IMPL), "exec"), globals())
for _name in (
    "_proxy_official_image_generation",
    "_proxy_post_request",
    "_send_local_responses_no_content",
    "_handle_websocket_recording_probe",
    "_reject_local_responses_websocket_probe",
    "_passthrough_official_control_request",
    "_send_user_requested_shutdown_outcome",
    "_send_json",
    "_send_json_and_close",
    "_relay_raw_upstream_response",
    "_send_sse_headers",
    "_write_sse_bytes",
    "_write_non_streaming_body_relay",
    "_write_sse_event",
    "_write_sse_data",
    "_write_sse_keepalive",
    "_write_sse_done",
    "_iter_upstream_sse_lines",
    "_iter_upstream_sse_events",
    "_write_sse_error_event",
    "_write_downstream_sse_error",
    "_write_sse_protocol_error_event",
    "_safe_send_downstream_json_error",
    "_safe_send_json",
    "_relay_official_passthrough_sse_response",
    "_relay_transparent_upstream_response",
    "_relay_upstream_response",
):
    setattr(CodexProxyHandler, _name, globals()[_name])


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
    shutdown_controller = GatewayShutdownController()
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
        writer_result = GATEWAY_EVENT_WRITER.shutdown(
            timeout=flush_timeout,
        )
        if not writer_result.completed:
            logger.warning("Gateway event writer shutdown ended with %s", writer_result.outcome)
        try:
            diagnostic_shutdown = getattr(GATEWAY_DIAGNOSTIC_RECORDER, "shutdown", None)
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
