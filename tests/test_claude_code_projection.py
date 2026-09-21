"""Claude Code discovery aliases for Gateway-exported models."""

from __future__ import annotations

import json
import re

import pytest

from claude_code_projection import (
    CLAUDE_DISCOVERY_RE,
    claude_model_list,
    projected_model_id,
    projection_map,
    resolve_projected_model_id,
)
from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, request_gateway


def test_projected_id_passes_claude_filter_and_round_trips() -> None:
    catalog = {
        "models": [
            {"slug": "volc/glm-5.2", "codex_proxy_metadata": {"provider": "volc"}},
            {"slug": "claude-sonnet", "codex_proxy_metadata": {"provider": "anthropic"}},
        ]
    }
    glm = projected_model_id("volc/glm-5.2")
    assert CLAUDE_DISCOVERY_RE.search(glm)
    assert glm != "volc/glm-5.2"
    assert projected_model_id("claude-sonnet") == "claude-sonnet"
    mapping = projection_map(catalog)
    assert mapping[glm] == "volc/glm-5.2"
    assert resolve_projected_model_id(glm, catalog) == "volc/glm-5.2"
    assert resolve_projected_model_id("volc/glm-5.2", catalog) == "volc/glm-5.2"
    ids = [row["id"] for row in claude_model_list(catalog)["data"]]
    assert glm in ids
    assert "claude-sonnet" in ids
    assert "volc/glm-5.2" not in ids


def test_projection_map_fails_closed_on_collision() -> None:
    catalog = {
        "models": [
            {"slug": "volc/glm-5.2"},
            {"slug": "volc-glm-5.2"},
        ]
    }
    with pytest.raises(Exception, match="collision"):
        projection_map(catalog)


@pytest.fixture
def harness() -> GatewayHarness:
    with GatewayHarness() as running:
        yield running


def test_models_without_anthropic_version_keep_canonical_ids(harness: GatewayHarness) -> None:
    response = request_gateway(harness.host, harness.port, "GET", "/v1/models")
    ids = [row["id"] for row in json.loads(response.body)["data"]]
    assert "volc/glm-5.2" in ids


def test_models_with_anthropic_version_project_every_id(harness: GatewayHarness) -> None:
    response = request_gateway(
        harness.host,
        harness.port,
        "GET",
        "/v1/models?limit=1000",
        headers={"anthropic-version": "2023-06-01", "Connection": "close"},
    )
    assert response.status == 200
    ids = [row["id"] for row in json.loads(response.body)["data"]]
    assert ids
    assert "volc/glm-5.2" not in ids
    assert all(CLAUDE_DISCOVERY_RE.search(model_id) for model_id in ids)
    projected = projected_model_id("volc/glm-5.2")
    assert projected in ids


def test_messages_accepts_projected_model_id(harness: GatewayHarness) -> None:
    harness.set_json_response(
        {
            "id": "chat_char_1",
            "object": "chat.completion",
            "model": "glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello-chat"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )
    projected = projected_model_id("volc/glm-5.2")
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/messages",
        body=json.dumps(
            {
                "model": projected,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hello"}],
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
            "Content-Type": "application/json",
            "Connection": "close",
        },
        timeout=8.0,
    )
    assert response.status == 200
    assert json.loads(response.body)["type"] == "message"
    assert harness.stub is not None
    sent = json.loads(harness.stub.captures[0].body)
    assert sent["model"] == "glm-5.2"
