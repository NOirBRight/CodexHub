from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_grok_provider_compat", ROOT / "scripts" / "e2e_grok_provider_compat.py"
)
assert SPEC and SPEC.loader
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)


def test_grok_e2e_covers_both_endpoints_for_each_provider() -> None:
    by_provider: dict[str, set[str]] = {}
    for case in E2E.CASES:
        by_provider.setdefault(case["provider_id"], set()).add(case["inbound"])
    assert by_provider["openai"] == {"responses", "chat_completions"}
    assert by_provider["opencode-go"] == {"responses", "chat_completions"}
    assert by_provider["commandcode"] == {"responses", "chat_completions"}
    paths = {case["endpoint"] for case in E2E.CASES}
    assert "/v1/providers/openai/responses" in paths
    assert "/v1/providers/openai/chat/completions" in paths


def test_grok_e2e_headers_stay_unknown_client() -> None:
    headers = E2E.grok_headers("gateway-key")
    assert "X-Codex-Client-Id" not in headers
    assert headers["Authorization"] == "Bearer gateway-key"


def test_grok_e2e_payloads_carry_system_and_flat_tools() -> None:
    responses = E2E.grok_responses_payload("gpt-5.6-luna", "SENTINEL:x")
    assert responses["input"][0]["role"] == "system"
    assert responses["tools"][0]["name"] == "read_file"
    assert "function" not in responses["tools"][0]
    chat = E2E.grok_chat_payload("gpt-5.6-luna", "SENTINEL:x")
    assert chat["messages"][0]["role"] == "system"
    assert chat["tools"][0]["name"] == "read_file"
    assert "function" not in chat["tools"][0]


def test_grok_e2e_protocol_failure_detects_luna_400() -> None:
    body = b'{"error":{"message":"Bad request (400): System messages are not allowed"}}'
    assert "System messages are not allowed" in E2E.protocol_failure(400, body)
    assert E2E.protocol_failure(200, b'{"id":"resp_1"}') == ""
    assert E2E.protocol_failure(200, b'{"tools":[{"parameters":{"additionalProperties":false}}]}') == ""
    assert "additionalProperties" in E2E.protocol_failure(
        400, b'{"error":{"message":"additionalProperties is required"}}'
    )
