from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_issue_64_collaboration_inventory.py"
INVENTORY = ROOT / "docs" / "evidence" / "issue-64" / "collaboration-v1-v2-inventory.json"
SOURCE_CONTRACT = (
    ROOT / "docs" / "evidence" / "issue-392" / "collaboration-runtime-contract.json"
)


def load_inventory_module():
    spec = importlib.util.spec_from_file_location("issue_64_collaboration_inventory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inventory_is_bound_to_the_accepted_issue392_runtime_contract() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert payload["schema"] == "codexhub.issue64.collaboration-v1-v2.v1"
    assert payload["artifact_kind"] == "collaboration_v1_v2_inventory"
    assert payload["qualification_status"] == "superseded_by_issue392_exact_runtime_contract"
    assert payload["candidate_identity"] == {
        "candidate_revision": "be10f62f44b22fa8c84510238250ae11fb3ecab4",
        "cli_version": "0.146.1",
        "cli_source_commit": "79b4f03d35962b005b007a015113b38930711665",
        "desktop_version": "26.803.5235.0",
        "desktop_runtime_version": "0.147.0-alpha.6.5",
        "desktop_source_commit": "618b8e9111da9f57fe380b09d0f6516e3f343536",
        "source_capture_status": "observed",
        "source_contract_file": "collaboration-runtime-contract.json",
    }
    assert payload["evidence_binding"]["basis"] == (
        "accepted_issue392_exact_runtime_contract"
    )
    assert len(payload["evidence_binding"]["source_contract"]["sha256"]) == 64


def test_inventory_covers_exact_v1_and_v2_shapes() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    protocols = {entry["id"]: entry for entry in payload["protocols"]}

    assert set(protocols) == {"collaboration_v1", "collaboration_v2"}
    for protocol in protocols.values():
        assert protocol["owner"] == protocol["executor"] == "codex_client"
        assert protocol["declaration"]["wire_type"] == "namespace"
        assert protocol["declaration"]["child_wire_type"] == "function"
        assert protocol["call"]["wire_type"] == "function_call"
        assert protocol["result"]["wire_type"] == "function_call_output"
        assert protocol["stream"]["event_order"] == [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
        ]
        assert protocol["stream"]["terminal_events"] == [
            "response.completed",
            "response.incomplete",
            "response.failed",
        ]

    v1 = protocols["collaboration_v1"]
    assert v1["namespace"] == "multi_agent_v1"
    assert v1["declaration"]["tool_names"] == [
        "close_agent",
        "resume_agent",
        "send_input",
        "spawn_agent",
        "wait_agent",
    ]
    assert v1["result"]["spawn_identity"] == "agent_id"
    assert v1["result"]["tool_output_schemas"]["spawn_agent"]["required"] == [
        "agent_id",
        "nickname",
    ]

    v2 = protocols["collaboration_v2"]
    assert v2["namespace"] == "collaboration"
    assert v2["declaration"]["tool_names"] == [
        "followup_task",
        "interrupt_agent",
        "list_agents",
        "send_message",
        "spawn_agent",
        "wait_agent",
    ]
    spawn = v2["declaration"]["argument_fields"]["spawn_agent"]
    assert spawn["required"] == ["message", "task_name"]
    assert spawn["optional"] == [
        "agent_type",
        "fork_turns",
        "model",
        "reasoning_effort",
    ]
    assert "service_tier" not in spawn["normalized_parameter_schema"]["properties"]
    assert v2["result"]["spawn_identity"] == "task_name"
    assert v2["history"]["same_home_restart_readback"] == "observed"


def test_boundary_requires_namespace_and_known_tool_before_argument_checks() -> None:
    module = load_inventory_module()

    assert (
        module.classify_boundary(
            {"namespace": "multi_agent_v1", "name": "spawn_agent"}
        )
        == "collaboration_v1"
    )
    assert (
        module.classify_boundary(
            {"namespace": "collaboration", "name": "followup_task"}
        )
        == "collaboration_v2"
    )
    assert (
        module.classify_boundary(
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "item_id": "<opaque>",
                "call_id": "<opaque>",
                "arguments": {"task_name": "<opaque>", "message": "<redacted>"},
            }
        )
        == "collaboration_v2"
    )

    cases = (
        ({"name": "spawn_agent"}, "boundary_discriminator_missing"),
        ({"namespace": "unknown", "name": "spawn_agent"}, "boundary_namespace_unknown"),
        ({"namespace": "collaboration", "name": "unknown"}, "boundary_tool_unknown"),
        (
            {"namespace": "collaboration", "name": "spawn_agent", "arguments": "{}"},
            "boundary_arguments_invalid",
        ),
        (
            {
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "tool_name": "wait_agent",
            },
            "boundary_discriminator_conflict",
        ),
        (
            {"namespace": "multi_agent_v1", "name": "spawn_agent", "unexpected": True},
            "boundary_fields_unknown",
        ),
        (
            {
                "namespace": "collaboration",
                "name": "wait_agent",
                "arguments": {"random": True},
            },
            "boundary_arguments_unknown",
        ),
    )
    for value, code in cases:
        with pytest.raises(module.InventoryValidationError, match=code):
            module.classify_boundary(value)


def test_boundary_rejects_v1_v2_mixed_fields() -> None:
    module = load_inventory_module()
    for value in (
        {
            "namespace": "multi_agent_v1",
            "name": "spawn_agent",
            "arguments": {"message": "<redacted>", "task_name": "child"},
        },
        {
            "namespace": "collaboration",
            "name": "spawn_agent",
            "arguments": {"message": "<redacted>", "fork_context": False},
        },
    ):
        with pytest.raises(
            module.InventoryValidationError, match="boundary_v1_v2_field_mixed"
        ):
            module.classify_boundary(value)


def test_inventory_declares_exact_boundary_and_adapter_requirements() -> None:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    boundary = payload["boundary"]
    assert boundary["matching_policy"] == "complete_namespace_and_child_schema"
    assert boundary["shared_tool_name_policy"] == "never_classify_from_child_name_alone"
    assert boundary["request_metadata_version_field"] == (
        "absent_in_both_frozen_runtimes"
    )
    assert boundary["tool_choice"] == "auto"
    for field in (
        "unknown_action",
        "missing_action",
        "duplicate_action",
        "conflicting_action",
        "mixed_v1_v2_action",
    ):
        assert boundary[field] == "fail_closed"

    requirements = payload["adapter_requirements"]
    assert requirements["owner"] == "codex_client"
    assert requirements["adaptation_owner"] == "codexhub_gateway"
    assert requirements["native_namespace"] == "pass_without_semantic_mutation"
    assert "preserve_call_and_item_identity" in requirements["requirements"]
    assert "preserve_agent_message_author_and_recipient" in requirements["requirements"]
    assert "execute_tools" in requirements["forbidden_gateway_actions"]
    assert "schedule_agents" in requirements["forbidden_gateway_actions"]
    assert "rewrite_history" in requirements["forbidden_gateway_actions"]


def test_inventory_reconciliation_and_replay_fail_closed() -> None:
    module = load_inventory_module()
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert module.reconcile_inventory(
        payload, source_contract_path=SOURCE_CONTRACT
    ) == {"reconciled": True, "mismatches": []}

    for case in ("mutation", "deletion", "loss"):
        report = module.reconcile_inventory(
            module.replay_inventory(payload, case), source_contract_path=SOURCE_CONTRACT
        )
        assert report == {"reconciled": False, "mismatches": ["inventory_content_invalid"]}

    mutated = copy.deepcopy(payload)
    mutated["protocols"][1]["stream"]["event_order"] = []
    assert module.reconcile_inventory(
        mutated, source_contract_path=SOURCE_CONTRACT
    ) == {"reconciled": False, "mismatches": ["inventory_content_invalid"]}


def test_source_contract_validation_and_digest_are_reproducible(tmp_path: Path) -> None:
    module = load_inventory_module()
    source = json.loads(SOURCE_CONTRACT.read_text(encoding="utf-8"))
    module._validate_source_contract(source)

    mutated = copy.deepcopy(source)
    mutated["selection_contract"]["markers"]["collaboration_v2"]["namespace"] = (
        "wrong"
    )
    with pytest.raises(
        module.InventoryValidationError, match="source_contract_content_invalid"
    ):
        module._validate_source_contract(mutated)

    canonical = SOURCE_CONTRACT.read_bytes().replace(b"\r\n", b"\n")
    lf_path = tmp_path / "source-lf.json"
    crlf_path = tmp_path / "source-crlf.json"
    lf_path.write_bytes(canonical)
    crlf_path.write_bytes(canonical.replace(b"\n", b"\r\n"))
    assert module._sha256_file(lf_path) == module._sha256_file(crlf_path)


def test_builder_check_succeeds() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
