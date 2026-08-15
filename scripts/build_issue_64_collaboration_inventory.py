#!/usr/bin/env python3
"""Build the #64 inventory from the exact runtime contract accepted by #392."""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "codexhub.issue64.collaboration-v1-v2.v1"
ARTIFACT_KIND = "collaboration_v1_v2_inventory"
DEFAULT_SOURCE_CONTRACT = Path(
    "docs/evidence/issue-392/collaboration-runtime-contract.json"
)
DEFAULT_OUTPUT = Path("docs/evidence/issue-64/collaboration-v1-v2-inventory.json")

V1_ID = "collaboration_v1"
V2_ID = "collaboration_v2"
V1_NAMESPACE = "multi_agent_v1"
V2_NAMESPACE = "collaboration"
V1_TOOLS = ("close_agent", "resume_agent", "send_input", "spawn_agent", "wait_agent")
V2_TOOLS = (
    "followup_task",
    "interrupt_agent",
    "list_agents",
    "send_message",
    "spawn_agent",
    "wait_agent",
)
V1_ONLY_FIELDS = frozenset({"agent_id", "fork_context", "items", "targets"})
V2_ONLY_FIELDS = frozenset({"task_name", "fork_turns", "path_prefix"})
V1_ONLY_TOOLS = frozenset({"send_input", "close_agent", "resume_agent"})
V2_ONLY_TOOLS = frozenset(
    {"send_message", "followup_task", "interrupt_agent", "list_agents"}
)
DEFERRED_QUALIFICATION = [
    "full_six_tool_two_child_lifecycle",
    "cross_home_negative_cases",
    "malformed_agent_message_and_identity_negative_cases",
]


class InventoryValidationError(ValueError):
    """Bounded inventory failure; never includes request content."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise InventoryValidationError(code)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryValidationError("source_contract_invalid") from error
    _require(isinstance(value, dict), "source_contract_invalid")
    return value


def _sha256_file(path: Path) -> str:
    try:
        canonical = path.read_bytes().replace(b"\r\n", b"\n")
    except OSError as error:
        raise InventoryValidationError("source_contract_unavailable") from error
    return hashlib.sha256(canonical).hexdigest()


def _issue392_module():
    path = Path(__file__).with_name("build_issue_392_collaboration_contract.py")
    spec = importlib.util.spec_from_file_location("issue392_contract", path)
    _require(spec is not None and spec.loader is not None, "source_validator_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_source_contract(source: Mapping[str, Any]) -> None:
    module = _issue392_module()
    try:
        module.validate_contract(source)
    except module.ContractValidationError as error:
        raise InventoryValidationError("source_contract_content_invalid") from error


def _argument_fields(source: Mapping[str, Any], version: str) -> dict[str, Any]:
    return copy.deepcopy(
        source["selection_contract"]["markers"][version]["argument_contract"]
    )


def _protocol_v1(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": V1_ID,
        "namespace": V1_NAMESPACE,
        "owner": "codex_client",
        "executor": "codex_client",
        "status": "runtime_observed",
        "shape_provenance": {
            "source": "issue392_exact_cli_and_desktop_runtime_contract",
            "cli_capture_status": "observed",
            "desktop_capture_status": "observed",
        },
        "declaration": {
            "wire_type": "namespace",
            "fields": ["type", "name", "description", "tools"],
            "child_wire_type": "function",
            "tool_names": list(V1_TOOLS),
            "argument_fields": _argument_fields(source, V1_ID),
        },
        "call": {
            "wire_type": "function_call",
            "fields": ["type", "id", "name", "namespace", "arguments", "call_id"],
            "identity_fields": ["id", "call_id"],
        },
        "result": {
            "wire_type": "function_call_output",
            "fields": ["type", "id", "call_id", "output"],
            "identity_fields": ["id", "call_id"],
            "spawn_identity": "agent_id",
            "output_wire_encoding": source["protocols"][V1_ID]["result"][
                "output_wire_encoding"
            ],
            "tool_output_schemas": copy.deepcopy(
                source["protocols"][V1_ID]["result"]["tool_output_schemas"]
            ),
        },
        "history": {
            "items": ["function_call", "function_call_output"],
            "identity_fields": ["id", "call_id", "agent_id"],
            "rollout_version_field": "multi_agent_version",
        },
        "stream": {
            "event_order": list(
                source["wire_lifecycle"]["function_call_stream"]["event_order"]
            ),
            "event_shapes": copy.deepcopy(
                source["wire_lifecycle"]["function_call_stream"]["event_shapes"]
            ),
            "terminal_events": list(source["wire_lifecycle"]["terminal"]["events"]),
            "terminal_shapes": copy.deepcopy(
                source["wire_lifecycle"]["terminal"]["event_shapes"]
            ),
        },
        "isolation": {
            "forbidden_v2_fields": sorted(V2_ONLY_FIELDS),
            "forbidden_v2_tools": sorted(V2_ONLY_TOOLS),
        },
    }


def _protocol_v2(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": V2_ID,
        "namespace": V2_NAMESPACE,
        "owner": "codex_client",
        "executor": "codex_client",
        "status": "runtime_observed_request_shape_and_readback",
        "shape_provenance": {
            "source": "issue392_exact_cli_and_desktop_runtime_contract",
            "cli_capture_status": "observed",
            "desktop_capture_status": "observed",
        },
        "declaration": {
            "wire_type": "namespace",
            "fields": ["type", "name", "description", "tools"],
            "child_wire_type": "function",
            "tool_names": list(V2_TOOLS),
            "argument_fields": _argument_fields(source, V2_ID),
        },
        "call": {
            "wire_type": "function_call",
            "fields": ["type", "id", "name", "namespace", "arguments", "call_id"],
            "identity_fields": ["id", "call_id"],
        },
        "result": {
            "wire_type": "function_call_output",
            "fields": ["type", "id", "call_id", "output"],
            "identity_fields": ["id", "call_id"],
            "spawn_identity": "task_name",
            "spawn_output_fields_default": ["task_name"],
            "continuation_id_field": "not_present",
            "output_wire_encoding": copy.deepcopy(
                source["protocols"][V2_ID]["result"]["output_wire_encoding"]
            ),
            "tool_output_schemas": copy.deepcopy(
                source["protocols"][V2_ID]["result"]["tool_output_schemas"]
            ),
        },
        "history": {
            "items": ["function_call", "function_call_output", "agent_message"],
            "identity_fields": ["id", "call_id", "author", "recipient"],
            "canonical_task_identity": "agent_path_serialized_as_task_name",
            "rollout_version_field": "multi_agent_version",
            "same_home_restart_readback": "observed",
        },
        "stream": {
            "event_order": list(
                source["wire_lifecycle"]["function_call_stream"]["event_order"]
            ),
            "event_shapes": copy.deepcopy(
                source["wire_lifecycle"]["function_call_stream"]["event_shapes"]
            ),
            "terminal_events": list(source["wire_lifecycle"]["terminal"]["events"]),
            "terminal_shapes": copy.deepcopy(
                source["wire_lifecycle"]["terminal"]["event_shapes"]
            ),
        },
        "isolation": {
            "forbidden_v1_fields": sorted(V1_ONLY_FIELDS),
            "forbidden_v1_tools": sorted(V1_ONLY_TOOLS),
        },
    }


def build_inventory(source_contract_path: Path = DEFAULT_SOURCE_CONTRACT) -> dict[str, Any]:
    source = _load_json(source_contract_path)
    _validate_source_contract(source)
    runtimes = source["runtimes"]
    return {
        "schema": SCHEMA,
        "artifact_kind": ARTIFACT_KIND,
        "verification_scope": "runtime_observed_structural_contract",
        "qualification_status": "superseded_by_issue392_exact_runtime_contract",
        "candidate_identity": {
            "candidate_revision": source["candidate_revision"],
            "cli_version": runtimes[0]["client_version"],
            "cli_source_commit": runtimes[0]["source_commit"],
            "desktop_version": runtimes[1]["client_version"],
            "desktop_runtime_version": runtimes[1]["runtime_version"],
            "desktop_source_commit": runtimes[1]["source_commit"],
            "source_capture_status": "observed",
            "source_contract_file": source_contract_path.name,
        },
        "evidence_binding": {
            "source_contract": {
                "file": source_contract_path.name,
                "sha256": _sha256_file(source_contract_path),
            },
            "basis": "accepted_issue392_exact_runtime_contract",
        },
        "boundary": {
            "pre_exposure_stage": "before_schema_repair_state_or_scheduler",
            "protocol_markers": {
                V1_ID: {"namespace": V1_NAMESPACE, "tool_names": list(V1_TOOLS)},
                V2_ID: {"namespace": V2_NAMESPACE, "tool_names": list(V2_TOOLS)},
            },
            "matching_policy": "complete_namespace_and_child_schema",
            "shared_tool_name_policy": "never_classify_from_child_name_alone",
            "direct_function_policy": "not_a_frozen_runtime_collaboration_marker",
            "request_metadata_version_field": "absent_in_both_frozen_runtimes",
            "tool_choice": "auto",
            "unknown_action": "fail_closed",
            "missing_action": "fail_closed",
            "duplicate_action": "fail_closed",
            "conflicting_action": "fail_closed",
            "mixed_v1_v2_action": "fail_closed",
        },
        "protocols": [_protocol_v1(source), _protocol_v2(source)],
        "protocol_layers": copy.deepcopy(source["protocol_layers"]),
        "adapter_requirements": {
            "owner": "codex_client",
            "adaptation_owner": "codexhub_gateway",
            "native_namespace": "pass_without_semantic_mutation",
            "adapt_only_when": "complete_lifecycle_inverse_is_proven",
            "requirements": [
                "injective_request_scoped_aliases",
                "preserve_namespace_and_child_name",
                "preserve_call_and_item_identity",
                "preserve_order_and_stream_boundaries",
                "preserve_task_name_and_agent_path_identity",
                "preserve_agent_message_author_and_recipient",
                "preserve_same_home_history_replay",
                "reject_unknown_missing_duplicate_conflicting_or_mixed_boundary",
                "keep_v1_and_v2_isolated",
            ],
            "forbidden_gateway_actions": [
                "execute_tools",
                "schedule_agents",
                "forge_results_or_identity",
                "downgrade_v2_to_v1",
                "rewrite_history",
            ],
        },
        "deferred_qualification": DEFERRED_QUALIFICATION,
    }


def classify_boundary(value: Mapping[str, Any]) -> str:
    """Classify an already-selected namespaced call without using name alone."""

    _require(isinstance(value, Mapping), "boundary_input_invalid")
    allowed = {"type", "id", "item_id", "call_id", "namespace", "name", "tool_name", "arguments"}
    _require(set(value).issubset(allowed), "boundary_fields_unknown")
    namespace = value.get("namespace")
    name = value.get("name", value.get("tool_name"))
    if "name" in value and "tool_name" in value:
        _require(value["name"] == value["tool_name"], "boundary_discriminator_conflict")
    _require(isinstance(namespace, str) and isinstance(name, str), "boundary_discriminator_missing")
    arguments = value.get("arguments", {})
    _require(isinstance(arguments, Mapping), "boundary_arguments_invalid")
    fields = set(arguments)
    if namespace == V1_NAMESPACE:
        _require(name in V1_TOOLS, "boundary_tool_unknown")
        _require(not fields.intersection(V2_ONLY_FIELDS), "boundary_v1_v2_field_mixed")
        version = V1_ID
    elif namespace == V2_NAMESPACE:
        _require(name in V2_TOOLS, "boundary_tool_unknown")
        _require(not fields.intersection(V1_ONLY_FIELDS), "boundary_v1_v2_field_mixed")
        version = V2_ID
    else:
        raise InventoryValidationError("boundary_namespace_unknown")
    source = _load_json(DEFAULT_SOURCE_CONTRACT)
    argument_contract = source["selection_contract"]["markers"][version][
        "argument_contract"
    ][name]
    required = argument_contract["required"]
    optional = argument_contract["optional"]
    _require(fields.issubset(set(required) | set(optional)), "boundary_arguments_unknown")
    return version


def validate_inventory(payload: Mapping[str, Any], source_contract_path: Path) -> None:
    expected = build_inventory(source_contract_path)
    _require(isinstance(payload, Mapping), "inventory_invalid")
    _require(payload.get("schema") == SCHEMA, "inventory_schema_invalid")
    _require(dict(payload) == expected, "inventory_content_invalid")


def reconcile_inventory(
    payload: Mapping[str, Any], *, source_contract_path: Path
) -> dict[str, Any]:
    try:
        validate_inventory(payload, source_contract_path)
    except InventoryValidationError as error:
        return {"reconciled": False, "mismatches": [str(error)]}
    return {"reconciled": True, "mismatches": []}


def replay_inventory(payload: Mapping[str, Any], case: str) -> dict[str, Any]:
    _require(case in {"mutation", "deletion", "loss"}, "replay_case_invalid")
    clone = copy.deepcopy(dict(payload))
    if case == "mutation":
        clone["protocols"][1]["namespace"] = V1_NAMESPACE
    elif case == "deletion":
        clone["protocols"] = clone["protocols"][:1]
    else:
        clone["candidate_identity"].pop("desktop_source_commit", None)
    return clone


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-contract", type=Path, default=DEFAULT_SOURCE_CONTRACT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--replay-case",
        choices=("identity", "mutation", "deletion", "loss"),
        default="identity",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inventory = build_inventory(args.source_contract)
        validate_inventory(inventory, args.source_contract)
        if args.replay_case != "identity":
            report = reconcile_inventory(
                replay_inventory(inventory, args.replay_case),
                source_contract_path=args.source_contract,
            )
            print(json.dumps(report, sort_keys=True))
            return 0 if not report["reconciled"] else 1
        if args.check:
            report = reconcile_inventory(
                _load_json(args.out), source_contract_path=args.source_contract
            )
            print(json.dumps(report, sort_keys=True))
            return 0 if report["reconciled"] else 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(inventory, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps({"schema": SCHEMA, "reconciled": True}, sort_keys=True))
        return 0
    except InventoryValidationError as error:
        print(f"INVENTORY_INVALID:{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
