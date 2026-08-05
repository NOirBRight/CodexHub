from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_issue_64_collaboration_inventory.py"
INVENTORY = ROOT / "docs" / "evidence" / "issue-64" / "collaboration-v1-v2-inventory.json"
SOURCE_CONTRACT = ROOT / "docs" / "evidence" / "issue-62" / "codex-0.146-source-contract.json"


def load_inventory_module():
    spec = importlib.util.spec_from_file_location("issue_64_collaboration_inventory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_is_bound_to_the_accepted_0146_source_contract() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert payload["schema"] == "codexhub.issue64.collaboration-v1-v2.v1"
    assert payload["artifact_kind"] == "collaboration_v1_v2_inventory"
    assert payload["qualification_status"] == "structural_boundary_only"
    assert payload["candidate_identity"] == {
        "cli_version": "0.146.0",
        "source_commit": "e363b08c9175ac1cbe5893615dd2cb9ddf95043b",
        "source_tag": "rust-v0.146.0",
        "source_commit_status": "published_attested",
        "source_capture_status": "not_observed",
        "source_contract_file": "codex-0.146-source-contract.json",
    }


def test_inventory_covers_bounded_v1_and_v2_shapes() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    protocols = {entry["id"]: entry for entry in payload["protocols"]}

    assert set(protocols) == {"collaboration_v1", "collaboration_v2"}
    for protocol in protocols.values():
        assert set(protocol) == {
            "id",
            "namespace",
            "owner",
            "executor",
            "status",
            "declaration",
            "call",
            "result",
            "history",
            "stream",
            "terminal_error",
            "isolation",
        }
        assert protocol["owner"] == protocol["executor"] == "codex_client"
        assert protocol["declaration"]["fields"]
        assert protocol["call"]["fields"]
        assert protocol["result"]["fields"]
        assert protocol["history"]["identity_fields"]
        assert protocol["stream"]["event_order"]
        assert protocol["terminal_error"]["terminal_events"]

    v1 = protocols["collaboration_v1"]
    assert v1["namespace"] == "multi_agent_v1"
    assert v1["declaration"]["tool_names"] == [
        "spawn_agent",
        "send_input",
        "wait_agent",
        "close_agent",
        "resume_agent",
    ]
    assert v1["history"]["continuation_identity"] == "agent_id"
    assert "task_path" in v1["isolation"]["forbidden_v2_fields"]
    assert "fork_turns" in v1["isolation"]["forbidden_v2_fields"]

    v2 = protocols["collaboration_v2"]
    assert v2["namespace"] == "collaboration"
    assert v2["declaration"]["tool_names"] == [
        "spawn_agent",
        "send_message",
        "followup_task",
        "wait_agent",
        "interrupt_agent",
        "list_agents",
    ]
    assert v2["history"]["continuation_identity"] == "task_path"
    assert v2["call"]["argument_fields"]["spawn_agent"]["optional"] == [
        "fork_turns",
        "agent_type",
        "model",
        "reasoning_effort",
    ]
    assert "agent_id" in v2["isolation"]["forbidden_v1_fields"]
    assert "close_agent" in v2["isolation"]["forbidden_v1_tools"]


def test_boundary_requires_exact_namespace_and_tool_discriminator() -> None:
    module = load_inventory_module()

    assert module.classify_boundary({"namespace": "multi_agent_v1", "name": "spawn_agent"}) == "collaboration_v1"
    assert module.classify_boundary({"namespace": "collaboration", "name": "followup_task"}) == "collaboration_v2"

    for value, code in (
        ({"name": "spawn_agent"}, "boundary_discriminator_missing"),
        ({"namespace": "unknown", "name": "spawn_agent"}, "boundary_namespace_unknown"),
        ({"namespace": "collaboration", "name": "unknown_tool"}, "boundary_tool_unknown"),
    ):
        with pytest.raises(module.InventoryValidationError, match=code):
            module.classify_boundary(value)


def test_boundary_rejects_v1_v2_mixed_fields_before_repair() -> None:
    module = load_inventory_module()

    mixed_v1 = {
        "namespace": "multi_agent_v1",
        "name": "spawn_agent",
        "arguments": {"message": "<redacted>", "task_path": "<opaque>"},
    }
    mixed_v2 = {
        "namespace": "collaboration",
        "name": "spawn_agent",
        "arguments": {"message": "<redacted>", "fork_context": False, "agent_id": "<opaque>"},
    }

    for value, code in ((mixed_v1, "boundary_v1_v2_field_mixed"), (mixed_v2, "boundary_v1_v2_field_mixed")):
        with pytest.raises(module.InventoryValidationError, match=code):
            module.classify_boundary(value)


def test_inventory_declares_adapter_requirements_without_implementing_lifecycle() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    requirements = payload["adapter_requirements"]

    assert requirements["owner"] == "codex_client"
    assert requirements["adaptation_owner"] == "codexhub_gateway"
    assert "namespace_to_function" in requirements["requirements"]
    assert "injective_request_scoped_aliases" in requirements["requirements"]
    assert "preserve_task_path_identity" in requirements["requirements"]
    assert "execute_tools" in requirements["forbidden_gateway_actions"]
    assert "schedule_agents" in requirements["forbidden_gateway_actions"]
    assert "forge_results" in requirements["forbidden_gateway_actions"]
    assert payload["deferred_qualification"] == [
        "full_spawn_message_followup_wait_interrupt_list_lifecycle",
        "restart_and_cold_root_resume",
        "cross_home_topology_and_history",
    ]


def test_inventory_reconciliation_fails_closed_on_unknown_or_mutated_shape() -> None:
    module = load_inventory_module()
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert module.reconcile_inventory(payload, source_contract_path=SOURCE_CONTRACT) == {
        "reconciled": True,
        "mismatches": [],
    }

    unknown = copy.deepcopy(payload)
    unknown["protocols"][0]["new_field"] = "must-fail"
    report = module.reconcile_inventory(unknown, source_contract_path=SOURCE_CONTRACT)
    assert report["reconciled"] is False
    assert any("protocol_fields_invalid" in item for item in report["mismatches"])

    ambiguous = copy.deepcopy(payload)
    ambiguous["boundary"]["discriminator_fields"] = ["name"]
    report = module.reconcile_inventory(ambiguous, source_contract_path=SOURCE_CONTRACT)
    assert report["reconciled"] is False
    assert any("boundary_discriminator_fields_invalid" in item for item in report["mismatches"])


def test_inventory_replay_controls_are_negative() -> None:
    module = load_inventory_module()
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))

    for case in ("mutation", "deletion", "loss"):
        replay = module.replay_inventory(payload, case)
        report = module.reconcile_inventory(replay, source_contract_path=SOURCE_CONTRACT)
        assert report["reconciled"] is False, case
        assert report["mismatches"], case
