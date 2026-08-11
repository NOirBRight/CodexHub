from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import codex_proxy
from codex_semantic_adapter import COLLABORATION_V1, COLLABORATION_V2
from collaboration_runtime_contract import (
    EXPECTED_OUTPUT_SCHEMAS,
    EXPECTED_PARAMETER_SCHEMAS,
    V2_NAMESPACE,
    V2_TOOLS,
)


_V2_ARGUMENTS = {
    "spawn_agent": {"task_name": "worker", "message": "do work", "fork_turns": "all"},
    "send_message": {"target": "/root/worker", "message": "status"},
    "followup_task": {"target": "/root/worker", "message": "continue"},
    "wait_agent": {"timeout_ms": 1000},
    "list_agents": {},
    "interrupt_agent": {"target": "/root/worker"},
}

_V2_RESULTS = {
    "spawn_agent": {"task_name": "/root/worker"},
    "send_message": None,
    "followup_task": None,
    "wait_agent": {"message": "done", "timed_out": False},
    "list_agents": {"agents": [{"agent_name": "/root/worker", "agent_status": "running"}]},
    "interrupt_agent": {"previous_status": "running"},
}


def _v2_declaration() -> dict[str, Any]:
    """Return the exact collaboration namespace declaration for V2."""
    children = []
    for name in sorted(V2_TOOLS):
        parameters = copy.deepcopy(EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2][name])
        if not parameters.get("required"):
            parameters.pop("required", None)
        children.append(
            {
                "type": "function",
                "name": name,
                "description": f"dynamic description for {name}",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": V2_NAMESPACE,
        "description": "dynamic namespace description",
        "tools": children,
    }


def _v2_history() -> list[dict[str, Any]]:
    """Return a canonical V2 history covering all six tools plus an agent_message."""
    history: list[dict[str, Any]] = []
    for index, name in enumerate(V2_TOOLS):
        call_id = f"call-{index}"
        arguments = json.dumps(_V2_ARGUMENTS[name], separators=(",", ":"))
        output = _V2_RESULTS[name]
        result_output = "null" if output is None else json.dumps(output, separators=(",", ":"))
        history.append(
            {
                "type": "function_call",
                "id": f"item-{call_id}",
                "call_id": call_id,
                "namespace": V2_NAMESPACE,
                "name": name,
                "arguments": arguments,
            }
        )
        history.append(
            {
                "type": "function_call_output",
                "id": f"result-{call_id}",
                "call_id": call_id,
                "output": result_output,
            }
        )
    history.append(
        {
            "type": "agent_message",
            "id": "agent-message-1",
            "author": "/root/worker",
            "recipient": "/root",
            "content": [
                {"type": "input_text", "text": "done"},
                {"type": "encrypted_content", "encrypted_content": "opaque"},
            ],
        }
    )
    return history


def _responses_upstream(*, native_namespace: bool) -> dict[str, object]:
    return {
        "name": "responses_fixture",
        "upstream_model": "fixture-model",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
        "tool_protocol_capabilities": {
            "function_lifecycle": True,
            "namespace_lifecycle": native_namespace,
            "accepts_namespace_adapter": True,
        },
    }


def _adapted_upstream() -> dict[str, object]:
    """Responses endpoint without namespace lifecycle; triggers alias adaptation."""
    return {
        "name": "adapted_fixture",
        "upstream_model": "fixture-model",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
        "tool_protocol_capabilities": {
            "function_lifecycle": True,
            "namespace_lifecycle": False,
            "accepts_namespace_adapter": True,
        },
    }


def _request_body(tools: list[dict] | None = None, *, input_items: list[dict] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "fixture-model",
        "tool_choice": "auto",
        "tools": tools if tools is not None else [_v2_declaration()],
    }
    if input_items is not None:
        body["input"] = input_items
    return body


class _ProtocolFixture:
    """Protocol-controlled upstream fixture.

    Records every request the gateway forwards and provides canned Responses
    bodies/SSE lines back through the same compatibility surface.  The fixture
    itself never executes a model, creates an agent, or switches providers.
    """

    def __init__(
        self,
        body: dict[str, Any],
        upstream: dict[str, Any],
        *,
        inject_codex_tools: bool = False,
        repair_policy: str | None = None,
    ) -> None:
        self.original_body = copy.deepcopy(body)
        self.upstream = upstream
        self.upstream_name = str(upstream["name"])
        self.inject_codex_tools = inject_codex_tools
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.sse_lines: list[bytes] = []
        self.cross_provider_requests = 0
        self.fallback_count = 0
        self.event_context: dict[str, Any] = {"request_id": "issue-283-fixture"}
        if repair_policy is not None:
            self.event_context["repair_policy"] = repair_policy

    def request(self) -> dict[str, Any]:
        """Run the request through the gateway and record what the upstream receives."""
        raw = json.dumps(self.original_body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        transformed = codex_proxy.compatible_request_body(
            raw,
            self.upstream,
            model_id="fixture-model",
            event_context=self.event_context,
            inject_codex_tools=self.inject_codex_tools,
        )
        payload = json.loads(transformed)
        self.requests.append(payload)
        if payload.get("model") != self.original_body.get("model"):
            self.fallback_count += 1
        if self.event_context.get("upstream_switched"):
            self.cross_provider_requests += 1
        return payload

    def response(self, body: dict[str, Any]) -> dict[str, Any]:
        """Run one upstream Responses body back through the gateway."""
        raw = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        transformed = codex_proxy.compatible_response_body(
            raw,
            self.upstream_name,
            event_context=self.event_context,
        )
        payload = json.loads(transformed)
        self.responses.append(payload)
        return payload

    def sse(self, line: bytes) -> bytes:
        """Run one upstream SSE line back through the gateway."""
        transformed = codex_proxy.compatible_sse_line(
            line,
            self.upstream_name,
            event_context=self.event_context,
        )
        self.sse_lines.append(transformed)
        return transformed

    def decode_sse(self, line: bytes) -> dict[str, Any] | None:
        """Decode a transformed SSE data line for assertions."""
        if not line.startswith(b"data:"):
            return None
        payload = json.loads(line.split(b":", 1)[1].strip())
        return payload

    @property
    def plan(self) -> Any:
        return self.event_context.get("_runtime_tool_compatibility_plan")


def _native_v2_sse_event(name: str, call_id: str, item_id: str, output_index: int) -> list[bytes]:
    """Return the four canonical SSE events for one native V2 function_call."""
    arguments = json.dumps(_V2_ARGUMENTS[name], separators=(",", ":"))
    item = {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "namespace": V2_NAMESPACE,
        "name": name,
        "arguments": "",
        "status": "in_progress",
    }
    done_item = {**item, "arguments": arguments, "status": "completed"}
    events = [
        {"type": "response.output_item.added", "output_index": output_index, "item": item},
        {
            "type": "response.function_call_arguments.delta",
            "output_index": output_index,
            "item_id": item_id,
            "delta": arguments,
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": output_index,
            "item_id": item_id,
            "arguments": arguments,
        },
        {"type": "response.output_item.done", "output_index": output_index, "item": done_item},
    ]
    return [
        f"data: {json.dumps(event, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8")
        for event in events
    ]


def _agent_message_sse_event(item_id: str, output_index: int) -> list[bytes]:
    item = {
        "type": "agent_message",
        "id": item_id,
        "author": "/root/worker",
        "recipient": "/root",
        "content": [
            {"type": "input_text", "text": "done"},
            {"type": "encrypted_content", "encrypted_content": "opaque"},
        ],
    }
    events = [
        {"type": "response.output_item.added", "output_index": output_index, "item": item},
        {"type": "response.output_item.done", "output_index": output_index, "item": item},
    ]
    return [
        f"data: {json.dumps(event, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8")
        for event in events
    ]


def test_c1_native_responses_forwards_declarations_unchanged() -> None:
    """C1: native Responses endpoint receives the V2 namespace unchanged."""
    fixture = _ProtocolFixture(_request_body(), _responses_upstream(native_namespace=True))
    payload = fixture.request()

    assert fixture.event_context["collaboration_protocol"] == COLLABORATION_V2
    assert fixture.event_context.get("subagent_spawn_allowed") is False
    assert payload["tools"] == [_v2_declaration()]
    assert payload["model"] == "fixture-model"
    assert fixture.cross_provider_requests == 0
    assert fixture.fallback_count == 0

    # Upstream emits native V2 lifecycle calls in response order.
    decoded_names: list[str] = []
    decoded_call_ids: list[str] = []
    for index, name in enumerate(V2_TOOLS):
        call_id = f"stream-call-{index}"
        item_id = f"stream-item-{index}"
        for event_line in _native_v2_sse_event(name, call_id, item_id, index):
            transformed = fixture.sse(event_line)
            event = fixture.decode_sse(transformed)
            assert event is not None
            if event["type"] == "response.output_item.done":
                item = event["item"]
                assert item["namespace"] == V2_NAMESPACE
                assert item["name"] == name
                assert item["call_id"] == call_id
                assert item["id"] == item_id
                decoded_names.append(item["name"])
                decoded_call_ids.append(item["call_id"])

    assert decoded_names == list(V2_TOOLS)
    assert len(set(decoded_call_ids)) == len(decoded_call_ids)

    # Parent final agent_message is forwarded unchanged.
    for event_line in _agent_message_sse_event("agent-msg-final", len(V2_TOOLS)):
        transformed = fixture.sse(event_line)
        event = fixture.decode_sse(transformed)
        assert event is not None
        if event["type"] == "response.output_item.done":
            assert event["item"]["type"] == "agent_message"
            assert event["item"]["author"] == "/root/worker"
            assert event["item"]["recipient"] == "/root"

    # A terminal response.completed is forwarded unchanged.
    terminal_items = []
    for index, name in enumerate(V2_TOOLS):
        arguments = json.dumps(_V2_ARGUMENTS[name], separators=(",", ":"))
        terminal_items.append(
            {
                "type": "function_call",
                "id": f"stream-item-{index}",
                "call_id": f"stream-call-{index}",
                "namespace": V2_NAMESPACE,
                "name": name,
                "arguments": arguments,
                "status": "completed",
            }
        )
    terminal_items.append(
        {
            "type": "agent_message",
            "id": "agent-msg-final",
            "author": "/root/worker",
            "recipient": "/root",
            "content": [
                {"type": "input_text", "text": "done"},
                {"type": "encrypted_content", "encrypted_content": "opaque"},
            ],
        }
    )
    terminal_line = (
        f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'resp-final', 'output': terminal_items}}, ensure_ascii=True, separators=(',', ':'))}\n\n"
    ).encode("utf-8")
    transformed = fixture.sse(terminal_line)
    terminal = fixture.decode_sse(transformed)
    assert terminal is not None
    assert terminal["type"] == "response.completed"
    assert [item["name"] for item in terminal["response"]["output"][:6]] == list(V2_TOOLS)
    assert terminal["response"]["output"][6]["type"] == "agent_message"


def test_c1_native_history_round_trips_unchanged() -> None:
    """C1: native V2 history survives request encoding and response decoding."""
    body = _request_body(input_items=_v2_history())
    fixture = _ProtocolFixture(body, _responses_upstream(native_namespace=True))
    payload = fixture.request()

    assert payload["input"] == body["input"]

    # Response body with the same history also round-trips unchanged.
    response = fixture.response({"id": "resp-history", "output": body["input"]})
    assert response["output"] == body["input"]


def test_c2_adapted_alias_encoding_is_injective_and_reversible() -> None:
    """C2: alias adapter produces reversible aliases with request-local mappings."""
    body_with_history = _request_body(input_items=_v2_history())
    fixture = _ProtocolFixture(body_with_history, _adapted_upstream())
    payload = fixture.request()

    assert fixture.event_context["collaboration_protocol"] == COLLABORATION_V2
    assert fixture.event_context.get("subagent_spawn_allowed") is False
    assert fixture.cross_provider_requests == 0
    assert fixture.fallback_count == 0

    aliases = [tool["name"] for tool in payload["tools"] if tool.get("type") == "function"]
    assert len(aliases) == len(V2_TOOLS)
    assert len(set(aliases)) == len(aliases)
    assert all(alias.startswith("__codexhub_ns_") for alias in aliases)
    assert not any(alias.startswith("multi_agent_v1__") for alias in aliases)

    # History is rewritten to aliases and arguments are preserved.
    encoded_calls = [
        item for item in payload["input"] if isinstance(item, dict) and item.get("type") == "function_call"
    ]
    assert len(encoded_calls) == len(V2_TOOLS)
    for call in encoded_calls:
        assert "namespace" not in call
        assert call["name"] in aliases
        decoded_args = json.loads(call["arguments"])
        assert "task_path" not in decoded_args
        assert "fork_context" not in decoded_args

    # Upstream returns aliases; gateway decodes back to namespace + original names.
    plan = fixture.plan
    assert plan is not None
    records = [plan.registry.record_for_alias(alias) for alias in aliases]
    assert all(record is not None for record in records)
    assert all(record.namespace == V2_NAMESPACE for record in records)
    assert all(record.child_name in V2_TOOLS for record in records)

    upstream_output = []
    for index, name in enumerate(V2_TOOLS):
        alias = plan.entries[0].aliases[index]
        upstream_output.append(
            {
                "type": "function_call",
                "id": f"adapted-item-{index}",
                "call_id": f"adapted-call-{index}",
                "name": alias,
                "arguments": json.dumps(_V2_ARGUMENTS[name], separators=(",", ":")),
            }
        )
    response = fixture.response({"id": "resp-adapted", "output": upstream_output})
    for index, item in enumerate(response["output"]):
        assert item["namespace"] == V2_NAMESPACE
        assert item["name"] == list(V2_TOOLS)[index]
        assert item["call_id"] == f"adapted-call-{index}"


def test_c2_adapted_aliases_are_stable_for_identical_requests() -> None:
    """C2: identical declaration shapes produce the same cache-stable aliases."""
    fixture_a = _ProtocolFixture(_request_body(), _adapted_upstream())
    payload_a = fixture_a.request()
    aliases_a = {tool["name"] for tool in payload_a["tools"] if tool.get("type") == "function"}

    fixture_b = _ProtocolFixture(_request_body(), _adapted_upstream())
    payload_b = fixture_b.request()
    aliases_b = {tool["name"] for tool in payload_b["tools"] if tool.get("type") == "function"}

    assert aliases_a == aliases_b
    assert payload_a == payload_b


def test_c3_stream_arguments_delta_assembly_and_terminal() -> None:
    """C3: adapted stream reassembles fragmented arguments and reconciles terminal."""
    fixture = _ProtocolFixture(_request_body(), _adapted_upstream())
    fixture.request()
    plan = fixture.plan
    assert plan is not None
    alias = plan.entries[0].aliases[list(V2_TOOLS).index("spawn_agent")]
    item_id = "delta-item"
    call_id = "delta-call"

    # Fragmented arguments arrive in two deltas.
    arguments = json.dumps(_V2_ARGUMENTS["spawn_agent"], separators=(",", ":"))
    half = len(arguments) // 2
    fixture.sse(
        f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'type': 'function_call', 'id': item_id, 'call_id': call_id, 'name': alias, 'arguments': '', 'status': 'in_progress'}}, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8")
    )
    fixture.sse(
        f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'output_index': 0, 'item_id': item_id, 'delta': arguments[:half]}, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8")
    )
    fixture.sse(
        f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'output_index': 0, 'item_id': item_id, 'delta': arguments[half:]}, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8")
    )
    fixture.sse(
        f"data: {json.dumps({'type': 'response.function_call_arguments.done', 'output_index': 0, 'item_id': item_id, 'arguments': arguments}, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8")
    )
    done_event = fixture.sse(
        f"data: {json.dumps({'type': 'response.output_item.done', 'output_index': 0, 'item': {'type': 'function_call', 'id': item_id, 'call_id': call_id, 'name': alias, 'arguments': arguments, 'status': 'completed'}}, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8")
    )
    decoded_done = fixture.decode_sse(done_event)
    assert decoded_done is not None
    assert decoded_done["item"]["name"] == "spawn_agent"
    assert decoded_done["item"]["namespace"] == V2_NAMESPACE
    assert decoded_done["item"]["arguments"] == arguments

    # Duplicate output_item.done for the same id is rejected (no fabricated completion).
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        fixture.sse(done_event)
    assert exc_info.value.cause.code in {
        codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE,
        "tool_compatibility_boundary",
    }

    # Terminal response.completed must match the assembled item (still on the alias wire).
    terminal_line = (
        f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'resp-stream', 'output': [{'type': 'function_call', 'id': item_id, 'call_id': call_id, 'name': alias, 'arguments': arguments, 'status': 'completed'}]}}, ensure_ascii=True, separators=(',', ':'))}\n\n"
    ).encode("utf-8")
    transformed = fixture.sse(terminal_line)
    terminal = fixture.decode_sse(transformed)
    assert terminal is not None
    assert terminal["type"] == "response.completed"
    assert terminal["response"]["output"][0]["name"] == "spawn_agent"
    assert terminal["response"]["output"][0]["namespace"] == V2_NAMESPACE


def test_c3_stream_preserves_output_order_and_no_reordering() -> None:
    """C3: adapted stream preserves the upstream output order."""
    fixture = _ProtocolFixture(_request_body(), _adapted_upstream())
    fixture.request()
    plan = fixture.plan
    assert plan is not None

    names = list(V2_TOOLS)
    for index, name in enumerate(names):
        alias = plan.entries[0].aliases[index]
        arguments = json.dumps(_V2_ARGUMENTS[name], separators=(",", ":"))
        item_id = f"order-item-{index}"
        call_id = f"order-call-{index}"
        for event in [
            {"type": "response.output_item.added", "output_index": index, "item": {"type": "function_call", "id": item_id, "call_id": call_id, "name": alias, "arguments": "", "status": "in_progress"}},
            {"type": "response.function_call_arguments.done", "output_index": index, "item_id": item_id, "arguments": arguments},
            {"type": "response.output_item.done", "output_index": index, "item": {"type": "function_call", "id": item_id, "call_id": call_id, "name": alias, "arguments": arguments, "status": "completed"}},
        ]:
            fixture.sse(f"data: {json.dumps(event, ensure_ascii=True, separators=(',', ':'))}\n\n".encode("utf-8"))

    terminal_line = (
        f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'resp-order', 'output': [
            {'type': 'function_call', 'id': f'order-item-{i}', 'call_id': f'order-call-{i}', 'name': plan.entries[0].aliases[i], 'arguments': json.dumps(_V2_ARGUMENTS[name], separators=(',', ':')), 'status': 'completed'}
            for i, name in enumerate(names)
        ]}}, ensure_ascii=True, separators=(',', ':'))}\n\n"
    ).encode("utf-8")
    terminal = fixture.decode_sse(fixture.sse(terminal_line))
    assert terminal is not None
    assert [item["name"] for item in terminal["response"]["output"]] == names


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        # Mixed V1/V2 tools.
        (
            lambda body: body["tools"].append(
                {"type": "namespace", "name": "multi_agent_v1", "tools": [{"type": "function", "name": "spawn_agent"}]}
            ),
            codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE,
        ),
        # Mixed V1/V2 history.
        (
            lambda body: body.setdefault("input", []).append(
                {
                    "type": "function_call",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "call_id": "mixed-call",
                    "arguments": '{"agent_type":"general","message":"work"}',
                }
            ),
            codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE,
        ),
        # V1-style parameters on a V2 function_call.
        (
            lambda body: body.setdefault("input", []).append(
                {
                    "type": "function_call",
                    "namespace": V2_NAMESPACE,
                    "name": "spawn_agent",
                    "call_id": "v1-style-call",
                    "arguments": '{"task_name":"worker","fork_context":true}',
                }
            ),
            "tool_compatibility_boundary",
        ),
        # Missing namespace on a native V2 spawn_agent.
        (
            lambda body: body.setdefault("input", []).append(
                {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "missing-ns-call",
                    "arguments": '{"task_name":"worker"}',
                }
            ),
            codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE,
        ),
        # Duplicate call_id.
        (
            lambda body: body.setdefault("input", []).extend([
                {
                    "type": "function_call",
                    "namespace": V2_NAMESPACE,
                    "name": "spawn_agent",
                    "call_id": "dup-call",
                    "arguments": '{"task_name":"a"}',
                },
                {
                    "type": "function_call",
                    "namespace": V2_NAMESPACE,
                    "name": "send_message",
                    "call_id": "dup-call",
                    "arguments": '{"target":"/root/a","message":"x"}',
                },
            ]),
            "tool_compatibility_boundary",
        ),
        # Missing call_id.
        (
            lambda body: body.setdefault("input", []).append(
                {
                    "type": "function_call",
                    "namespace": V2_NAMESPACE,
                    "name": "spawn_agent",
                    "arguments": '{"task_name":"worker"}',
                }
            ),
            "tool_compatibility_boundary",
        ),
        # Malformed agent_message content.
        (
            lambda body: body.setdefault("input", []).append(
                {
                    "type": "agent_message",
                    "id": "bad-msg",
                    "author": "/root/worker",
                    "recipient": "/root",
                    "content": [{"type": "output_text", "text": "wrong variant"}],
                }
            ),
            "tool_compatibility_boundary",
        ),
    ],
    ids=[
        "mixed_v1_v2_tools",
        "mixed_v1_v2_history",
        "v1_parameters_on_v2_call",
        "missing_namespace_spawn",
        "duplicate_call_id",
        "missing_call_id",
        "malformed_agent_message",
    ],
)
def test_c4_negative_cases_fail_before_mutation(mutate, expected_code: str) -> None:
    """C4: malformed or mixed V2 input is rejected before upstream sampling."""
    body = _request_body(input_items=[])
    mutate(body)
    fixture = _ProtocolFixture(body, _responses_upstream(native_namespace=False))

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        fixture.request()

    assert exc_info.value.cause.code == expected_code
    assert len(fixture.requests) == 0
    assert fixture.cross_provider_requests == 0
    assert fixture.fallback_count == 0


@pytest.mark.parametrize(
    "mutate",
    [
        # Ambiguous task identity: result claiming a collaboration call id that has no matching call.
        lambda history: history.append(
            {
                "type": "function_call_output",
                "id": "orphan-result",
                "call_id": "orphan-call",
                "output": '{"task_name":"/root/other"}',
            }
        ),
        # Cross-Home state: a V1 result mixed into V2 history.
        lambda history: history.extend([
            {
                "type": "function_call",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "call_id": "cross-home-call",
                "arguments": '{"agent_type":"general","message":"work"}',
            },
            {
                "type": "function_call_output",
                "call_id": "cross-home-call",
                "output": '{"agent_id":"x"}',
            },
        ]),
        # Incompatible paginated state: list_agents result with pagination fields.
        lambda history: history.extend([
            {
                "type": "function_call",
                "namespace": V2_NAMESPACE,
                "name": "list_agents",
                "call_id": "page-call",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "page-call",
                "output": '{"agents":[],"next_cursor":"opaque","has_more":true}',
            },
        ]),
    ],
    ids=["orphan_result", "cross_home_v1_state", "incompatible_paginated_state"],
)
def test_c4_history_integrity_and_state_fail_before_mutation(mutate) -> None:
    """C4: ambiguous identity or foreign state is rejected before sampling."""
    history: list[dict[str, Any]] = []
    mutate(history)
    body = _request_body(input_items=history)
    fixture = _ProtocolFixture(body, _responses_upstream(native_namespace=False))

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        fixture.request()

    assert exc_info.value.cause.code in {
        codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE,
        "tool_compatibility_boundary",
    }
    assert len(fixture.requests) == 0


def test_gateway_does_not_fabricate_completion_or_output() -> None:
    """The gateway never invents function_call_output or completion items."""
    fixture = _ProtocolFixture(_request_body(input_items=_v2_history()), _adapted_upstream())
    payload = fixture.request()

    # No function_call_output appears in the forwarded request unless present in input.
    input_types = [item.get("type") for item in payload.get("input", []) if isinstance(item, dict)]
    assert "function_call_output" not in input_types or any(
        item.get("type") == "function_call_output" for item in fixture.original_body.get("input", [])
    )

    # No completion item is synthesized in response handling.
    response = fixture.response({"id": "resp-empty", "output": []})
    assert response == {"id": "resp-empty", "output": []}


def test_v2_skips_v1_scheduler_and_repair() -> None:
    """V2 contexts do not initialize V1 scheduler/repair state."""
    fixture = _ProtocolFixture(
        _request_body(input_items=_v2_history()),
        _responses_upstream(native_namespace=False),
        repair_policy=codex_proxy.REPAIR_CODEX_SUBAGENT,
    )
    fixture.request()

    assert fixture.event_context.get("collaboration_protocol") == COLLABORATION_V2
    assert fixture.event_context.get("_subagent_state") is None
    assert fixture.event_context.get("_worker_stream_binding_state") is None
    assert fixture.event_context.get("subagent_spawn_allowed") is False
