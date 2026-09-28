"""HTTP contracts for external providers that speak Anthropic Messages."""

from __future__ import annotations

import json
from unittest.mock import patch

import gateway_catalog_runtime
from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, request_gateway
from tests.gateway_harness import server as harness_server


def test_external_anthropic_upstream_keeps_responses_error_shape() -> None:
    def write_truncated_response(handler, stub) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "256")
        handler.send_header("Connection", "close")
        handler.end_headers()
        stub.headers_sent.set()
        handler.wfile.write(b'{"type":"message","usage":{"input_tokens":1}')
        handler.wfile.flush()

    with GatewayHarness() as harness:
        assert harness.stub is not None
        stub = harness.stub
        upstream = {
            "name": "opencode-go",
            "provider_id": "opencode-go",
            "model_id": "opencode-go/union-alpha",
            "base_url": harness.stub_base_url,
            "auth": "api_key",
            "api_key": "synthetic-external-provider-key",
            "upstream_model": "union-alpha",
            "upstream_format": "auto",
            "tool_protocol": "auto",
            "tool_surface_strategy": "eager",
        }
        with (
            patch.object(gateway_catalog_runtime, "choose_upstream", return_value=upstream),
            patch.object(harness_server._StubHandler, "_write_response", write_truncated_response),
        ):
            response = request_gateway(
                harness.host,
                harness.port,
                "POST",
                "/v1/responses",
                body=json.dumps(
                    {
                        "model": "opencode-go/union-alpha",
                        "input": "hello",
                        "stream": False,
                    }
                ).encode(),
                headers={
                    "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
                timeout=8.0,
            )

    assert stub.captures
    assert stub.captures[0].path.endswith("/messages")
    assert response.status >= 400
    payload = json.loads(response.body)
    assert payload.get("type") != "error", payload
    assert isinstance(payload.get("error"), str), payload
