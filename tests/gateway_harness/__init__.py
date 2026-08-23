"""HTTP-level black-box characterization helpers for the Gateway.

Tests drive the production Gateway as a black box: HTTP in, HTTP/SSE out.
Private members of ``CodexProxyHandler`` are out of scope here.
"""

from tests.gateway_harness.client import GatewayHttpResponse, request_gateway
from tests.gateway_harness.server import (
    GATEWAY_CLIENT_KEY,
    GatewayHarness,
    StubCapture,
    StubUpstream,
)
from tests.gateway_harness.sse import (
    RESPONSES_TERMINAL_EVENTS,
    parsed_sse_events,
    require_single_terminal,
)

__all__ = [
    "GATEWAY_CLIENT_KEY",
    "GatewayHarness",
    "GatewayHttpResponse",
    "RESPONSES_TERMINAL_EVENTS",
    "StubCapture",
    "StubUpstream",
    "parsed_sse_events",
    "request_gateway",
    "require_single_terminal",
]
