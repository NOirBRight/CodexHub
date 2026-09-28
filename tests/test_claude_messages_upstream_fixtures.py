"""Protocol fixture coverage stays offline and model-free."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from anthropic_messages_ir import AdaptedResponse, adapt_upstream_response
from claude_messages_evidence_exchange import execute_exchange
from claude_messages_loopback_harness import _strace_verdict  # noqa: E402
from claude_messages_upstream_fixtures import UpstreamFixtureServer, fixture_response


@pytest.mark.parametrize("protocol", ["responses", "chat_completions", "anthropic_messages"])
def test_fixture_text_json_adapts_for_each_upstream(protocol: str) -> None:
    status, content_type, body = fixture_response(protocol, scenario="text")
    result = adapt_upstream_response(protocol, body, status=status, content_type=content_type)
    assert isinstance(result, AdaptedResponse)
    assert b"loopback response" in result.body


@pytest.mark.parametrize("protocol", ["responses", "chat_completions", "anthropic_messages"])
def test_fixture_tool_stream_keeps_call_identity(protocol: str) -> None:
    status, content_type, body = fixture_response(protocol, scenario="tool", stream=True)
    result = adapt_upstream_response(protocol, body, status=status, content_type=content_type)
    assert isinstance(result, AdaptedResponse)
    assert b"call_fixture_1" in result.body


def test_fixture_error_is_terminal_without_success_payload() -> None:
    status, content_type, body = fixture_response("responses", scenario="error")
    result = adapt_upstream_response("responses", body, status=status, content_type=content_type)
    assert isinstance(result, AdaptedResponse)
    payload = json.loads(result.body)
    assert payload["type"] == "error"
    assert "content" not in payload


def test_loopback_fixture_server_runs_through_admitted_exchange() -> None:
    server = UpstreamFixtureServer(("127.0.0.1", 0), scenario="tool")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    attempts: list[tuple[str, str, str, bytes]] = []
    try:
        body = json.dumps(
            {
                "model": "claude-fixture",
                "max_tokens": 128,
                "stream": True,
                "messages": [{"role": "user", "content": "fixture"}],
            },
            separators=(",", ":"),
        ).encode()
        result = execute_exchange(
            body,
            upstream_format="responses",
            url=f"http://127.0.0.1:{server.server_port}/v1/responses",
            admit=lambda protocol, method, url, exact_body: attempts.append((protocol, method, url, exact_body)),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert isinstance(result, AdaptedResponse)
    assert b"call_fixture_1" in result.body
    assert attempts and attempts[0][0:2] == ("responses", "POST")
    assert server.records == [{"protocol": "responses", "path": "/v1/responses", "keys": ["input", "max_output_tokens", "model", "stream"], "stream": True}]


def test_fixture_unknown_protocol_refuses() -> None:
    try:
        fixture_response("unknown")
    except ValueError as exc:
        assert "unsupported fixture protocol" in str(exc)
    else:
        raise AssertionError("unknown fixture protocol must refuse")


def test_strace_verdict_rejects_public_and_unknown_ipv6(tmp_path: Path) -> None:
    log = tmp_path / "connects.log"
    log.write_text(
        '\n'.join(
            [
                'connect(3, {sa_family=AF_INET, sin_addr=inet_addr("127.0.0.1")}, 16) = 0',
                'connect(4, {sa_family=AF_INET6, inet_pton(AF_INET6, "::1")}, 28) = 0',
                'connect(5, {sa_family=AF_INET6, inet_pton(AF_INET6, "2001:db8::1")}, 28) = 0',
                'connect(6, {sa_family=AF_INET6}, 28) = 0',
                'connect(7, {sa_family=AF_UNIX}, 110) = 0',
            ]
        ),
        encoding="utf-8",
    )
    verdict = _strace_verdict(log)
    assert verdict["connect_calls"] == 5
    assert verdict["non_loopback"] == [
        'connect(5, {sa_family=AF_INET6, inet_pton(AF_INET6, "2001:db8::1")}, 28) = 0',
        'connect(6, {sa_family=AF_INET6}, 28) = 0',
    ]
