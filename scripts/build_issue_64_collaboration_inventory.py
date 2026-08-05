#!/usr/bin/env python3
"""Build and reconcile the bounded Issue #64 Collaboration V1/V2 contract.

This is a structural inventory only.  It binds the accepted Codex CLI 0.146
source contract, records the pre-exposure discriminator and the two small
schema families, and states the namespace adapter requirements needed by #198
and #58.  It does not inspect a live client, execute tools, schedule agents,
or implement a protocol adapter.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "codexhub.issue64.collaboration-v1-v2.v1"
ARTIFACT_KIND = "collaboration_v1_v2_inventory"
DEFAULT_SOURCE_CONTRACT = Path("docs/evidence/issue-62/codex-0.146-source-contract.json")
DEFAULT_OUTPUT = Path("docs/evidence/issue-64/collaboration-v1-v2-inventory.json")

V1_ID = "collaboration_v1"
V2_ID = "collaboration_v2"
V1_NAMESPACE = "multi_agent_v1"
V2_NAMESPACE = "collaboration"
V1_TOOLS = ("spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent")
V2_TOOLS = (
    "spawn_agent",
    "send_message",
    "followup_task",
    "wait_agent",
    "interrupt_agent",
    "list_agents",
)
V1_ONLY_FIELDS = frozenset({"agent_id", "fork_context"})
V2_ONLY_FIELDS = frozenset({"task_name", "task_path", "fork_turns", "continuation_id"})
V1_ONLY_TOOLS = frozenset({"send_input", "close_agent", "resume_agent"})
V2_ONLY_TOOLS = frozenset({"send_message", "followup_task", "interrupt_agent", "list_agents"})
DEFERRED_QUALIFICATION = [
    "full_spawn_message_followup_wait_interrupt_list_lifecycle",
    "restart_and_cold_root_resume",
    "cross_home_topology_and_history",
]


class InventoryValidationError(ValueError):
    """A deliberately bounded, non-sensitive contract failure."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise InventoryValidationError(code)


def _exact(value: Any, fields: set[str], code: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), code)
    _require(set(value) == fields, code)
    return value


def _sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise InventoryValidationError("source_contract_unavailable") from error


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryValidationError("source_contract_invalid") from error
    _require(isinstance(value, dict), "source_contract_root_invalid")
    return value


def _validate_source_contract(source_contract: Mapping[str, Any]) -> None:
    _require(source_contract.get("fixture_kind") == "codex_cli_source_contract", "source_contract_kind_invalid")
    _require(source_contract.get("capture_status") == "not_observed", "source_contract_capture_status_invalid")
    provenance = source_contract.get("provenance")
    _require(isinstance(provenance, Mapping), "source_contract_provenance_invalid")
    _require(provenance.get("cli_version") == "0.146.0", "source_contract_cli_version_invalid")
    _require(provenance.get("source_commit") == "e363b08c9175ac1cbe5893615dd2cb9ddf95043b", "source_contract_commit_invalid")
    _require(provenance.get("cli_source_tag") == "rust-v0.146.0", "source_contract_tag_invalid")
    _require(provenance.get("cli_source_commit_status") == "published_attested", "source_contract_commit_status_invalid")
    surface = source_contract.get("runtime_wire_surface")
    _require(isinstance(surface, Mapping), "source_contract_surface_invalid")
    families = surface.get("declaration_family_order")
    _require(isinstance(families, list) and "namespace" in families, "source_contract_namespace_family_missing")


def _boundary_arguments(value: Mapping[str, Any]) -> Mapping[str, Any]:
    arguments = value.get("arguments")
    return arguments if isinstance(arguments, Mapping) else {}


def classify_boundary(value: Mapping[str, Any]) -> str:
    """Classify a declaration/call before exposure or semantic repair.

    The namespace and child tool name are both required.  A bare or flattened
    name is intentionally not enough to choose a repair path.
    """

    _require(isinstance(value, Mapping), "boundary_input_invalid")
    namespace = value.get("namespace")
    name = value.get("name", value.get("tool_name"))
    _require(isinstance(namespace, str) and namespace, "boundary_discriminator_missing")
    _require(isinstance(name, str) and name, "boundary_discriminator_missing")
    arguments = _boundary_arguments(value)
    fields = set(arguments)
    if namespace == V1_NAMESPACE:
        if fields & V2_ONLY_FIELDS or name in V2_ONLY_TOOLS:
            raise InventoryValidationError("boundary_v1_v2_field_mixed")
        if name not in V1_TOOLS:
            raise InventoryValidationError("boundary_tool_unknown")
        return V1_ID
    if namespace == V2_NAMESPACE:
        if fields & V1_ONLY_FIELDS or name in V1_ONLY_TOOLS:
            raise InventoryValidationError("boundary_v1_v2_field_mixed")
        if name not in V2_TOOLS:
            raise InventoryValidationError("boundary_tool_unknown")
        return V2_ID
    raise InventoryValidationError("boundary_namespace_unknown")


def _stream_shape() -> dict[str, Any]:
    return {
        "event_order": [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
        ],
        "terminal_events": ["response.completed", "response.incomplete", "response.failed"],
        "error_event": "response.failed",
        "qualification": "not_observed",
    }


def _protocol_v1() -> dict[str, Any]:
    return {
        "id": V1_ID,
        "namespace": V1_NAMESPACE,
        "owner": "codex_client",
        "executor": "codex_client",
        "status": "legacy_structural_contract",
        "declaration": {
            "discriminator": {"namespace": V1_NAMESPACE, "tool_field": "name"},
            "wire_type": "function",
            "fields": ["type", "name", "parameters"],
            "tool_names": list(V1_TOOLS),
            "flattened_name_prefixes": ["multi_agent_v1__", "multi_agent_v1."],
        },
        "call": {
            "wire_type": "function_call",
            "fields": ["type", "namespace", "name", "item_id", "call_id", "arguments"],
            "identity_fields": ["call_id", "item_id"],
            "argument_fields": {
                "spawn_agent": {"required": ["message"], "optional": ["agent_type", "model", "reasoning", "nickname", "fork_context"]},
                "send_input": {"required": ["target", "message"], "optional": ["interrupt"]},
                "wait_agent": {"required": ["targets"], "optional": ["timeout_ms"]},
                "close_agent": {"required": ["target"], "optional": []},
                "resume_agent": {"required": ["target", "message"], "optional": []},
            },
        },
        "result": {
            "wire_type": "function_call_output",
            "fields": ["type", "call_id", "item_id", "output"],
            "identity_fields": ["call_id", "item_id"],
            "result_identity": "agent_id",
            "result_fields_by_tool": {
                "spawn_agent": ["agent_id", "nickname"],
                "wait_agent": ["timed_out", "status"],
                "close_agent": ["previous_status"],
                "send_input": ["status"],
                "resume_agent": ["status"],
            },
        },
        "history": {
            "items": ["function_call", "function_call_output"],
            "identity_fields": ["agent_id", "call_id", "call_item_id", "output_item_id"],
            "continuation_identity": "agent_id",
            "transcript_markers": [
                "Previous real Codex native multi_agent_v1.<tool> call transcript",
                "Codex native multi_agent_v1.<tool> result",
            ],
        },
        "stream": _stream_shape(),
        "terminal_error": {
            "terminal_events": ["response.completed", "response.incomplete", "response.failed"],
            "error_event": "response.failed",
            "error_fields": ["id", "status", "error"],
            "qualification": "not_observed",
        },
        "isolation": {
            "forbidden_v2_fields": sorted(V2_ONLY_FIELDS),
            "forbidden_v2_tools": sorted(V2_ONLY_TOOLS),
            "forbidden_v2_semantics": ["task_path_continuation", "fork_turns"],
        },
    }


def _protocol_v2() -> dict[str, Any]:
    return {
        "id": V2_ID,
        "namespace": V2_NAMESPACE,
        "owner": "codex_client",
        "executor": "codex_client",
        "status": "stable_surface_not_lifecycle_qualified",
        "declaration": {
            "discriminator": {"namespace": V2_NAMESPACE, "tool_field": "name"},
            "wire_type": "namespace",
            "fields": ["type", "name", "tools"],
            "child_wire_type": "function",
            "tool_names": list(V2_TOOLS),
        },
        "call": {
            "wire_type": "function_call",
            "fields": ["type", "namespace", "name", "item_id", "call_id", "arguments"],
            "identity_fields": ["call_id", "item_id", "task_path", "continuation_id"],
            "argument_fields": {
                "spawn_agent": {"required": ["task_name", "message"], "optional": ["fork_turns", "agent_type", "model", "reasoning_effort"]},
                "send_message": {"required": ["target", "message"], "optional": []},
                "followup_task": {"required": ["target", "message"], "optional": []},
                "wait_agent": {"required": [], "optional": ["timeout_ms"]},
                "interrupt_agent": {"required": ["target"], "optional": []},
                "list_agents": {"required": [], "optional": ["path_prefix"]},
            },
            "fork_turns": {"field": "fork_turns", "values": ["all", "none", "positive_decimal_string"]},
        },
        "result": {
            "wire_type": "function_call_output",
            "fields": ["type", "call_id", "item_id", "output"],
            "identity_fields": ["call_id", "item_id", "task_path", "continuation_id"],
            "result_identity": "task_path",
            "result_fields": ["task_path", "continuation_id", "status"],
        },
        "history": {
            "items": ["function_call", "function_call_output"],
            "identity_fields": ["task_path", "continuation_id", "call_id", "call_item_id", "output_item_id"],
            "continuation_identity": "task_path",
            "continuation_fields": ["task_path", "continuation_id", "task_name", "fork_turns"],
            "pagination": "deferred_qualification",
            "inherited_prefix": "deferred_qualification",
        },
        "stream": {
            **_stream_shape(),
            "call_semantics": "namespace_child_function_uses_function_call_lifecycle",
        },
        "terminal_error": {
            "terminal_events": ["response.completed", "response.incomplete", "response.failed"],
            "error_event": "response.failed",
            "error_fields": ["id", "status", "error"],
            "qualification": "not_observed",
        },
        "isolation": {
            "forbidden_v1_fields": sorted(V1_ONLY_FIELDS),
            "forbidden_v1_tools": sorted(V1_ONLY_TOOLS),
            "forbidden_v1_semantics": ["agent_id_close_resume", "fork_context"],
        },
    }


def _build_inventory(source_contract_path: Path) -> dict[str, Any]:
    source_contract = _load_json(source_contract_path)
    _validate_source_contract(source_contract)
    return {
        "schema": SCHEMA,
        "artifact_kind": ARTIFACT_KIND,
        "verification_scope": "structural_contract_only",
        "qualification_status": "structural_boundary_only",
        "candidate_identity": {
            "cli_version": "0.146.0",
            "source_commit": "e363b08c9175ac1cbe5893615dd2cb9ddf95043b",
            "source_tag": "rust-v0.146.0",
            "source_commit_status": "published_attested",
            "source_capture_status": "not_observed",
            "source_contract_file": source_contract_path.name,
        },
        "evidence_binding": {
            "source_contract": {"file": source_contract_path.name, "sha256": _sha256_file(source_contract_path)},
            "basis": "accepted_issue_62_source_contract_and_repository_evidence",
        },
        "boundary": {
            "pre_exposure_stage": "before_tool_exposure_or_semantic_repair",
            "discriminator_fields": ["namespace", "name"],
            "protocol_markers": {
                V1_ID: {"namespace": V1_NAMESPACE, "tool_names": list(V1_TOOLS)},
                V2_ID: {"namespace": V2_NAMESPACE, "tool_names": list(V2_TOOLS)},
            },
            "flattened_name_policy": "not_decidable_without_namespace",
            "unknown_action": "fail_closed",
            "ambiguous_action": "fail_closed",
            "mixed_v1_v2_action": "fail_closed",
        },
        "protocols": [_protocol_v1(), _protocol_v2()],
        "adapter_requirements": {
            "owner": "codex_client",
            "adaptation_owner": "codexhub_gateway",
            "requirements": [
                "namespace_to_function",
                "injective_request_scoped_aliases",
                "preserve_task_path_identity",
                "preserve_call_result_history_links",
                "assemble_stream_before_validation",
                "reject_unknown_or_ambiguous_boundary",
                "keep_v1_and_v2_isolated",
            ],
            "forbidden_gateway_actions": ["execute_tools", "schedule_agents", "forge_results", "downgrade_v2_to_v1"],
            "scope": "requirements_only_for_198_and_58",
        },
        "deferred_qualification": list(DEFERRED_QUALIFICATION),
    }


def _validate_protocol(protocol: Any, expected_id: str, expected_namespace: str) -> None:
    expected_fields = {
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
    _exact(protocol, expected_fields, "protocol_fields_invalid")
    _require(protocol["id"] == expected_id and protocol["namespace"] == expected_namespace, "protocol_identity_invalid")
    _require(protocol["owner"] == "codex_client" and protocol["executor"] == "codex_client", "protocol_owner_invalid")
    declaration = protocol["declaration"]
    _require(isinstance(declaration, Mapping), "declaration_invalid")
    _require(isinstance(declaration.get("fields"), list) and declaration["fields"], "declaration_fields_invalid")
    tools = declaration.get("tool_names")
    expected_tools = V1_TOOLS if expected_id == V1_ID else V2_TOOLS
    _require(tools == list(expected_tools), "declaration_tool_names_invalid")
    for key in ("call", "result", "history"):
        section = protocol[key]
        _require(isinstance(section, Mapping), f"{key}_invalid")
        _require(isinstance(section.get("fields", section.get("identity_fields")), list), f"{key}_fields_invalid")
    stream = protocol["stream"]
    _require(isinstance(stream, Mapping) and stream.get("event_order") == _stream_shape()["event_order"], "stream_shape_invalid")
    terminal_error = protocol["terminal_error"]
    _require(isinstance(terminal_error, Mapping), "terminal_error_invalid")
    _require(terminal_error.get("error_event") == "response.failed", "terminal_error_event_invalid")
    _require(terminal_error.get("terminal_events") == ["response.completed", "response.incomplete", "response.failed"], "terminal_events_invalid")
    isolation = protocol["isolation"]
    _require(isinstance(isolation, Mapping), "isolation_invalid")
    if expected_id == V1_ID:
        _require(isolation.get("forbidden_v2_fields") == sorted(V2_ONLY_FIELDS), "v1_isolation_invalid")
        _require(isolation.get("forbidden_v2_tools") == sorted(V2_ONLY_TOOLS), "v1_isolation_invalid")
    else:
        _require(isolation.get("forbidden_v1_fields") == sorted(V1_ONLY_FIELDS), "v2_isolation_invalid")
        _require(isolation.get("forbidden_v1_tools") == sorted(V1_ONLY_TOOLS), "v2_isolation_invalid")


def validate_inventory(payload: Mapping[str, Any], source_contract_path: Path) -> None:
    _exact(
        payload,
        {
            "schema",
            "artifact_kind",
            "verification_scope",
            "qualification_status",
            "candidate_identity",
            "evidence_binding",
            "boundary",
            "protocols",
            "adapter_requirements",
            "deferred_qualification",
        },
        "inventory_fields_invalid",
    )
    _require(payload["schema"] == SCHEMA, "inventory_schema_invalid")
    _require(payload["artifact_kind"] == ARTIFACT_KIND, "inventory_kind_invalid")
    _require(payload["verification_scope"] == "structural_contract_only", "inventory_scope_invalid")
    _require(payload["qualification_status"] == "structural_boundary_only", "inventory_qualification_invalid")
    _validate_source_contract(_load_json(source_contract_path))

    candidate = _exact(
        payload["candidate_identity"],
        {
            "cli_version",
            "source_commit",
            "source_tag",
            "source_commit_status",
            "source_capture_status",
            "source_contract_file",
        },
        "candidate_fields_invalid",
    )
    _require(
        dict(candidate)
        == {
            "cli_version": "0.146.0",
            "source_commit": "e363b08c9175ac1cbe5893615dd2cb9ddf95043b",
            "source_tag": "rust-v0.146.0",
            "source_commit_status": "published_attested",
            "source_capture_status": "not_observed",
            "source_contract_file": source_contract_path.name,
        },
        "candidate_identity_invalid",
    )
    binding = _exact(payload["evidence_binding"], {"source_contract", "basis"}, "evidence_binding_invalid")
    source_binding = _exact(binding["source_contract"], {"file", "sha256"}, "evidence_binding_source_invalid")
    _require(source_binding["file"] == source_contract_path.name, "evidence_binding_source_file_invalid")
    _require(source_binding["sha256"] == _sha256_file(source_contract_path), "evidence_binding_source_digest_invalid")
    _require(binding["basis"] == "accepted_issue_62_source_contract_and_repository_evidence", "evidence_binding_basis_invalid")

    boundary = _exact(
        payload["boundary"],
        {
            "pre_exposure_stage",
            "discriminator_fields",
            "protocol_markers",
            "flattened_name_policy",
            "unknown_action",
            "ambiguous_action",
            "mixed_v1_v2_action",
        },
        "boundary_fields_invalid",
    )
    _require(boundary["discriminator_fields"] == ["namespace", "name"], "boundary_discriminator_fields_invalid")
    _require(boundary["unknown_action"] == "fail_closed" and boundary["ambiguous_action"] == "fail_closed", "boundary_fail_closed_invalid")
    markers = _exact(boundary["protocol_markers"], {V1_ID, V2_ID}, "boundary_markers_invalid")
    _require(markers[V1_ID] == {"namespace": V1_NAMESPACE, "tool_names": list(V1_TOOLS)}, "boundary_v1_marker_invalid")
    _require(markers[V2_ID] == {"namespace": V2_NAMESPACE, "tool_names": list(V2_TOOLS)}, "boundary_v2_marker_invalid")
    _require(boundary["flattened_name_policy"] == "not_decidable_without_namespace", "boundary_flattened_policy_invalid")

    protocols = payload["protocols"]
    _require(isinstance(protocols, list) and len(protocols) == 2, "protocols_invalid")
    _validate_protocol(protocols[0], V1_ID, V1_NAMESPACE)
    _validate_protocol(protocols[1], V2_ID, V2_NAMESPACE)
    _require(classify_boundary({"namespace": V1_NAMESPACE, "name": "spawn_agent"}) == V1_ID, "boundary_v1_not_decidable")
    _require(classify_boundary({"namespace": V2_NAMESPACE, "name": "spawn_agent"}) == V2_ID, "boundary_v2_not_decidable")

    adapter = _exact(payload["adapter_requirements"], {"owner", "adaptation_owner", "requirements", "forbidden_gateway_actions", "scope"}, "adapter_requirements_invalid")
    _require(adapter["owner"] == "codex_client" and adapter["adaptation_owner"] == "codexhub_gateway", "adapter_owner_invalid")
    _require(adapter["scope"] == "requirements_only_for_198_and_58", "adapter_scope_invalid")
    _require(isinstance(adapter["requirements"], list) and "namespace_to_function" in adapter["requirements"], "adapter_requirements_list_invalid")
    _require(
        adapter["forbidden_gateway_actions"] == ["execute_tools", "schedule_agents", "forge_results", "downgrade_v2_to_v1"],
        "adapter_forbidden_actions_invalid",
    )
    _require(payload["deferred_qualification"] == DEFERRED_QUALIFICATION, "deferred_qualification_invalid")


def reconcile_inventory(payload: Mapping[str, Any], *, source_contract_path: Path) -> dict[str, Any]:
    mismatches: list[str] = []
    try:
        validate_inventory(payload, source_contract_path)
    except InventoryValidationError as error:
        mismatches.append(str(error))
    return {"reconciled": not mismatches, "mismatches": mismatches}


def replay_inventory(payload: Mapping[str, Any], case: str) -> dict[str, Any]:
    _require(case in {"mutation", "deletion", "loss"}, "replay_case_invalid")
    clone = copy.deepcopy(dict(payload))
    if case == "mutation":
        clone["protocols"][1]["namespace"] = V1_NAMESPACE
    elif case == "deletion":
        clone["protocols"] = clone["protocols"][:1]
    else:
        clone["candidate_identity"].pop("source_commit", None)
    return clone


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-contract", type=Path, default=DEFAULT_SOURCE_CONTRACT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--replay-case", choices=("identity", "mutation", "deletion", "loss"), default="identity")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inventory = _build_inventory(args.source_contract)
        validate_inventory(inventory, args.source_contract)
        if args.replay_case != "identity":
            report = reconcile_inventory(replay_inventory(inventory, args.replay_case), source_contract_path=args.source_contract)
            print(json.dumps(report, sort_keys=True))
            return 0 if not report["reconciled"] else 1
        if args.check:
            existing = _load_json(args.out)
            report = reconcile_inventory(existing, source_contract_path=args.source_contract)
            if not report["reconciled"]:
                print(json.dumps(report, sort_keys=True))
                return 1
            return 0
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(inventory, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"schema": SCHEMA, "reconciled": True}, sort_keys=True))
        return 0
    except InventoryValidationError as error:
        print(f"INVENTORY_INVALID:{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
