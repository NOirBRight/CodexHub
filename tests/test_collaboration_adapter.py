"""Direct seam tests for the Collaboration adapter contract slice."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import pytest

import collaboration_adapter
import gateway_errors
from collaboration_adapter import (
    COLLABORATION_BOUNDARY_ERROR_CODE,
    WORKER_BINDING_ERROR_CODE,
    WORKER_REQUESTED_BINDING_FIELD,
    WORKER_SELECTOR_ERROR_CODE,
    CollaborationAdapter,
    CollaborationFacts,
    PathBindingSigner,
)
from codex_semantic_adapter import COLLABORATION_V2


FORBIDDEN_SOURCE_MARKERS = (
    "import codex_proxy",
    "from codex_proxy",
    "gateway_sse",
    "transport",
    "urllib3",
    "urlopen",
    "BaseHTTPRequestHandler",
)


def _adapter(tmp_path: Path, events: list | None = None) -> tuple[CollaborationAdapter, list]:
    captured = events if events is not None else []

    def emit(event: str, **fields):
        captured.append((event, fields))

    adapter = CollaborationAdapter(
        facts=CollaborationFacts(signing_root=tmp_path),
        emit=emit,
        signer=PathBindingSigner(tmp_path),
    )
    return adapter, captured


def _spawn_call(*, arguments, call_id="call_worker", item_id="item_worker", name="multi_agent_v1__spawn_agent"):
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def test_collaboration_adapter_typed_context_is_the_seam():
    assert {
        "facts",
        "emit",
        "signer",
    } <= set(CollaborationAdapter.__annotations__)
    source = Path(collaboration_adapter.__file__).read_text(encoding="utf-8")
    for marker in FORBIDDEN_SOURCE_MARKERS:
        assert marker not in source
    assert inspect.isclass(CollaborationFacts)
    assert inspect.isclass(PathBindingSigner)


def test_resolve_boundary_selects_v2_and_mutates_context_dict(tmp_path):
    adapter, events = _adapter(tmp_path)
    context = {}
    payload = {
        "input": [
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "call_id": "call_v2",
                "arguments": {
                    "task_name": "worker-task",
                    "message": "return the bounded result",
                    "fork_turns": "all",
                },
            }
        ],
    }
    protocol = adapter.resolve_boundary(payload, context)
    assert protocol == COLLABORATION_V2
    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert events == []


def test_mixed_history_fails_closed_without_payload_leak(tmp_path):
    adapter, events = _adapter(tmp_path)
    payload = {
        "input": [
            _spawn_call(arguments={"message": "SECRET_PROMPT", "fork_context": True}),
            {
                "type": "function_call",
                "call_id": "call_v2",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "arguments": {"task_name": "SECRET_TASK"},
            },
        ]
    }
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as raised:
        adapter.resolve_boundary(payload, {})
    assert raised.value.cause.code == COLLABORATION_BOUNDARY_ERROR_CODE
    assert events == [
        ("collaboration_boundary_rejected", {"surface": "request", "outcome": "rejected", "count": 1})
    ]
    assert "SECRET_PROMPT" not in repr(events)
    assert "SECRET_TASK" not in repr(events)


def test_chat_caller_rejects_worker_selector_before_sidecar_attach(tmp_path):
    adapter, events = _adapter(tmp_path)
    value = _spawn_call(arguments={"agent_type": "worker", "message": "do work", "fork_context": True})
    context = {"inbound_format": "chat_completions", "_worker_requested_binding": {
        "agent_type": "worker",
        "model": "glm-5.2",
        "reasoning": "high",
    }}
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as raised:
        adapter.apply_external_worker_response_contract(
            value,
            context,
            surface="body",
        )
    assert raised.value.cause.code == WORKER_SELECTOR_ERROR_CODE
    assert events == [
        (
            "worker_selector_validated",
            {
                "outcome": "rejected",
                "classification": "unsupported_caller_carrier",
                "surface": "body",
            },
        )
    ]
    assert WORKER_REQUESTED_BINDING_FIELD not in value
    assert WORKER_REQUESTED_BINDING_FIELD not in json.loads(json.dumps(value.get("arguments")))


def test_v2_context_bypasses_worker_sidecars_and_stream_ledger(tmp_path):
    adapter, events = _adapter(tmp_path)
    value = _spawn_call(arguments={"agent_type": "worker", "message": "do work"})
    context = {
        "collaboration_protocol": COLLABORATION_V2,
        "_worker_binding_required": True,
        "_worker_requested_binding": {
            "agent_type": "worker",
            "model": "glm-5.2",
            "reasoning": "high",
        },
    }
    rewritten, changed = adapter.apply_external_worker_response_contract(
        value,
        context,
        surface="sse",
    )
    assert rewritten is value
    assert changed is False
    adapter.raise_on_invalid_stream_event(
        {"type": "response.output_item.done", "item": value},
        context,
        surface="sse",
    )
    assert events == []
    assert "_worker_stream_binding_state" not in context
    assert WORKER_REQUESTED_BINDING_FIELD not in value


def test_hmac_fail_closed_rejects_tampered_sidecar(tmp_path):
    adapter, events = _adapter(tmp_path)
    requested = {"agent_type": "worker", "model": "glm-5.2", "reasoning": "high"}
    sidecar = adapter.requested_binding_sidecar(requested, "call_worker")
    sidecar["signature"] = "00" * 32
    verified, classification = adapter.verified_requested_binding(sidecar, "call_worker")
    assert verified is None
    assert classification == "unknown_requested_binding_sidecar"

    payload = {
        "input": [
            _spawn_call(
                arguments=json.dumps(
                    {
                        "agent_type": "worker",
                        "message": "do work",
                        "fork_context": True,
                        "model": None,
                        WORKER_REQUESTED_BINDING_FIELD: sidecar,
                    }
                )
            ),
            {
                "type": "function_call_output",
                "call_id": "call_worker",
                "output": json.dumps({"agent_id": "agent_1", "nickname": "w"}),
            },
        ]
    }
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as raised:
        adapter.validate_worker_binding_history(payload)
    assert raised.value.cause.code == WORKER_BINDING_ERROR_CODE
    assert events[-1][0] == "worker_requested_binding_validated"
    assert events[-1][1]["classification"] == "unknown_requested_binding_sidecar"


def test_stream_ledger_rejects_malformed_selector_before_sidecar(tmp_path):
    adapter, events = _adapter(tmp_path)
    context = {
        "_worker_binding_required": True,
        "_worker_requested_binding": {
            "agent_type": "worker",
            "model": "glm-5.2",
            "reasoning": "high",
        },
    }
    added = {
        "type": "response.output_item.added",
        "item": {
            **_spawn_call(arguments=""),
            "status": "in_progress",
        },
    }
    adapter.remember_stream_event(added, context)
    adapter.remember_stream_event(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_worker",
            "delta": '{"agent_type":',
        },
        context,
    )
    done = {
        "type": "response.function_call_arguments.done",
        "item_id": "item_worker",
        "arguments": '{"agent_type":',
    }
    adapter.remember_stream_event(done, context)
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as raised:
        adapter.raise_on_invalid_stream_event(done, context, surface="sse")
    assert raised.value.cause.code == WORKER_SELECTOR_ERROR_CODE
    assert events == [
        (
            "worker_selector_validated",
            {
                "outcome": "rejected",
                "classification": "malformed_arguments",
                "surface": "sse",
            },
        )
    ]
    rewritten, changed = adapter.attach_requested_binding_sidecars(
        added["item"],
        context,
        capture_stream_event=False,
    )
    assert changed is False
    arguments = rewritten.get("arguments")
    if isinstance(arguments, str) and arguments:
        parsed = json.loads(arguments)
        assert WORKER_REQUESTED_BINDING_FIELD not in parsed
    else:
        assert arguments in (None, "")


def test_facade_wrappers_use_live_emit_and_signing_root(tmp_path, monkeypatch):
    import codex_proxy

    seen = []

    def emit(event, **fields):
        seen.append((event, fields))

    monkeypatch.setattr(codex_proxy, "write_proxy_event", emit)
    monkeypatch.setattr(codex_proxy, "WORKER_BINDING_SIGNING_ROOT", tmp_path)
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError):
        codex_proxy._resolve_collaboration_boundary(
            {
                "input": [
                    _spawn_call(arguments={"message": "one", "fork_context": True}),
                    {
                        "type": "function_call",
                        "call_id": "call_v2",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                        "arguments": {"task_name": "two"},
                    },
                ]
            },
            {},
        )
    assert seen == [
        ("collaboration_boundary_rejected", {"surface": "request", "outcome": "rejected", "count": 1})
    ]
    adapter = codex_proxy._collaboration_adapter()
    assert adapter.facts.signing_root == tmp_path
