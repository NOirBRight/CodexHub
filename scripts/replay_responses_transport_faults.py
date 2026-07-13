"""Run sanitized deterministic Responses transport faults through a loopback Gateway.

The harness starts an in-process Gateway on an ephemeral localhost port and
replaces only its upstream opener.  It neither contacts a provider nor reads or
writes shared Codex configuration, credentials, or event logs.  Its output is
an aggregate suitable for regression tests and CI diagnostics.

Example red-capable loop before the Issue #114 fix::

    python scripts/replay_responses_transport_faults.py \
        --route official external --fault partial_incomplete_read \
        --retry-attempts 3 --expect-terminal
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from http.client import HTTPConnection, RemoteDisconnected
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Any, Iterable
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src-python"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import codex_proxy
from http.client import IncompleteRead


ROUTES = ("official", "external")
FAULTS = (
    "open_remote_disconnected",
    "open_timeout",
    "open_winerror_10053",
    "open_winerror_10054",
    "partial_incomplete_read",
    "post_open_winerror_10053",
    "post_open_winerror_10054",
    "healthy_long_stream",
)
POST_OPEN_FAULTS = {
    "partial_incomplete_read",
    "post_open_winerror_10053",
    "post_open_winerror_10054",
}

EXTERNAL_MODEL = {
    "alias": "volc/glm-5.2",
    "provider_alias": "volc",
    "upstream_name": "volcengine",
    "display_prefix": "Volc",
    "base_url": "https://fault-loop.invalid/v1",
    "api_key": "fault-loop-token",
    "upstream_model": "glm-5.2",
    "priority_base": 200,
    "context_window": 1_024_000,
    "max_output_tokens": 4096,
    "input_modalities": ("text",),
    "context_source": "fault_loop",
    "max_output_source": "fault_loop",
}


@dataclass(frozen=True)
class FaultOutcome:
    route: str
    fault: str
    http_status: int | None
    client_error: str | None
    response_terminal: str | None
    request_terminal: str | None
    request_terminal_status: int | None
    retry_event_count: int
    retry_max_attempts: int | None
    upstream_open_calls: int
    sse_line_count: int
    response_bytes: int

    @property
    def protocol_valid_terminal(self) -> bool:
        return self.response_terminal in {"response.completed", "response.failed"}

    @property
    def expected_request_terminal(self) -> str:
        return "request_complete" if self.fault == "healthy_long_stream" else "request_error"

    @property
    def terminalization_valid(self) -> bool:
        if self.request_terminal != self.expected_request_terminal:
            return False
        if self.fault.startswith("open_"):
            # Before downstream SSE begins, a normal HTTP error response is the
            # protocol terminal.  It must not be mistaken for a silent SSE path.
            return self.http_status is not None and self.http_status >= 400 and self.response_terminal is None
        expected_response_terminal = "response.completed" if self.fault == "healthy_long_stream" else "response.failed"
        return self.http_status == 200 and self.response_terminal == expected_response_terminal


def _sse_event(event_type: str, payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(
        {"type": event_type, **payload}, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8") + b"\n\n"


def _fault_exception(fault: str) -> BaseException:
    if fault == "open_remote_disconnected":
        return RemoteDisconnected("fault-loop upstream disconnected")
    if fault == "open_timeout":
        return TimeoutError("fault-loop upstream write timeout")
    if fault in {"open_winerror_10053", "post_open_winerror_10053"}:
        return ConnectionAbortedError(10053, "fault-loop connection aborted")
    if fault in {"open_winerror_10054", "post_open_winerror_10054"}:
        return ConnectionResetError(10054, "fault-loop connection reset")
    if fault == "partial_incomplete_read":
        return IncompleteRead(b"")
    raise ValueError(f"unsupported fault: {fault}")


class _InjectedSseResponse:
    """A finite upstream stream with a controllable opening or body failure."""

    status = 200
    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Transfer-Encoding": "chunked",
    }

    def __init__(self, fault: str):
        self._lines = self._lines_for_fault(fault)

    @staticmethod
    def _lines_for_fault(fault: str) -> list[bytes | BaseException]:
        if fault == "healthy_long_stream":
            lines: list[bytes | BaseException] = [
                _sse_event(
                    "response.created",
                    {"response": {"id": "resp_fault_loop", "status": "in_progress"}},
                )
            ]
            for index in range(3_249):
                lines.append(
                    _sse_event(
                        "response.output_text.delta",
                        {"output_index": 0, "delta": f"{index % 10}"},
                    )
                )
            lines.extend(
                [
                    _sse_event(
                        "response.completed",
                        {
                            "response": {
                                "id": "resp_fault_loop",
                                "status": "completed",
                                "output": [],
                            }
                        },
                    ),
                    b"",
                ]
            )
            return lines

        if fault not in POST_OPEN_FAULTS:
            raise ValueError(f"{fault} is not a stream fault")
        return [
            _sse_event(
                "response.created",
                {"response": {"id": "resp_fault_loop", "status": "in_progress"}},
            ),
            _fault_exception(fault),
        ]

    def readline(self) -> bytes:
        value = self._lines.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def __enter__(self) -> "_InjectedSseResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return False


def _request_for_route(route: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    if route == "official":
        return (
            "/v1/responses",
            {"X-Codex-Client-Id": "codex-app"},
            {
                "model": "gpt-5.5-fast",
                "input": "fault-loop request",
                "stream": True,
            },
        )
    if route == "external":
        return (
            "/v1/providers/volc/responses",
            {"X-Codex-Client-Id": "codex-app"},
            {
                "model": "volc/glm-5.2",
                "input": "fault-loop request",
                "stream": True,
            },
        )
    raise ValueError(f"unsupported route: {route}")


def _response_terminal(body: bytes) -> str | None:
    if b"event: response.completed" in body or b'"type":"response.completed"' in body:
        return "response.completed"
    if b"event: response.failed" in body or b'"type":"response.failed"' in body:
        return "response.failed"
    return None


def run_fault(route: str, fault: str, *, retry_attempts: int = 3) -> FaultOutcome:
    """Run one route/fault pair through the actual loopback HTTP handler."""

    if route not in ROUTES:
        raise ValueError(f"unsupported route: {route}")
    if fault not in FAULTS:
        raise ValueError(f"unsupported fault: {fault}")
    if retry_attempts < 1:
        raise ValueError("retry_attempts must be positive")

    events: list[tuple[str, dict[str, Any]]] = []
    upstream_open_calls = 0
    policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
    request_path, route_headers, payload = _request_for_route(route)

    def open_upstream(*args: Any, **kwargs: Any) -> _InjectedSseResponse:
        nonlocal upstream_open_calls
        upstream_open_calls += 1
        if fault.startswith("open_"):
            raise _fault_exception(fault)
        return _InjectedSseResponse(fault)

    def record_event(event: str, **fields: Any) -> None:
        events.append((event, fields))

    with tempfile.TemporaryDirectory(prefix="codexhub-fault-loop-") as temporary_directory:
        runtime_dir = Path(temporary_directory)
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {"CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS": str(retry_attempts)},
                    clear=False,
                )
            )
            stack.enter_context(patch("codex_proxy._runtime_settings_value", return_value=None))
            stack.enter_context(patch("codex_proxy.RUNTIME_CODEX_DIR", runtime_dir))
            stack.enter_context(patch("codex_proxy.RUNTIME_PROXY_DIR", runtime_dir / "proxy"))
            # Inject below the Gateway retry wrapper so open failures exercise the
            # production retry budget and telemetry instead of bypassing them.
            stack.enter_context(patch("codex_proxy._open_upstream_once", side_effect=open_upstream))
            stack.enter_context(patch("codex_proxy.write_proxy_event", side_effect=record_event))
            stack.enter_context(patch("codex_proxy.codex_access_token", return_value="fault-loop-token"))
            stack.enter_context(patch("codex_proxy.codex_account_id", return_value="fault-loop-account"))
            stack.enter_context(patch("codex_proxy.time.sleep", return_value=None))
            stack.enter_context(
                patch("codex_proxy.generated_catalog_slugs", return_value={"volc/glm-5.2"})
            )
            stack.enter_context(
                patch(
                    "codex_proxy.generated_catalog_by_slug",
                    return_value={"volc/glm-5.2": {"slug": "volc/glm-5.2", "max_output_tokens": 4096}},
                )
            )
            stack.enter_context(
                patch("codex_proxy.resolve_external_model_alias", return_value=EXTERNAL_MODEL)
            )
            stack.enter_context(
                patch(
                    "codex_proxy.load_policy",
                    return_value=replace(
                        policy,
                        allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2",),
                    ),
                )
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), codex_proxy.CodexProxyHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    **route_headers,
                }
                connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                connection.request("POST", request_path, body=body, headers=headers)
                try:
                    response = connection.getresponse()
                    response_body = response.read()
                    http_status: int | None = response.status
                    client_error = None
                except (OSError, TimeoutError) as exc:
                    response_body = b""
                    http_status = None
                    client_error = type(exc).__name__
                finally:
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)

    retry_events = [fields for event, fields in events if event == "upstream_retry"]
    request_events = [
        (event, fields)
        for event, fields in events
        if event in {"request_complete", "request_error"}
    ]
    request_terminal, request_fields = request_events[-1] if request_events else (None, {})
    max_attempt_values = [
        value
        for value in (fields.get("max_attempts") for fields in retry_events)
        if isinstance(value, int)
    ]
    return FaultOutcome(
        route=route,
        fault=fault,
        http_status=http_status,
        client_error=client_error,
        response_terminal=_response_terminal(response_body),
        request_terminal=request_terminal,
        request_terminal_status=request_fields.get("status")
        if isinstance(request_fields.get("status"), int)
        else None,
        retry_event_count=len(retry_events),
        retry_max_attempts=max(max_attempt_values) if max_attempt_values else None,
        upstream_open_calls=upstream_open_calls,
        sse_line_count=response_body.count(b"\n"),
        response_bytes=len(response_body),
    )


def run_fault_matrix(
    routes: Iterable[str], faults: Iterable[str], *, retry_attempts: int = 3
) -> list[FaultOutcome]:
    return [
        run_fault(route, fault, retry_attempts=retry_attempts)
        for route in routes
        for fault in faults
    ]


def _parse_selected(value: list[str], choices: tuple[str, ...]) -> tuple[str, ...]:
    selected = tuple(item for item in value if item != "all")
    return choices if not selected else selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=("all", *ROUTES), nargs="+", default=["all"])
    parser.add_argument("--fault", choices=("all", *FAULTS), nargs="+", default=["all"])
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument(
        "--expect-terminal",
        action="store_true",
        help="fail unless each result has the correct HTTP/SSE and request terminal state",
    )
    args = parser.parse_args(argv)
    outcomes = run_fault_matrix(
        _parse_selected(args.route, ROUTES),
        _parse_selected(args.fault, FAULTS),
        retry_attempts=args.retry_attempts,
    )
    print(json.dumps({"outcomes": [asdict(outcome) for outcome in outcomes]}, sort_keys=True))
    if args.expect_terminal and not all(outcome.terminalization_valid for outcome in outcomes):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
