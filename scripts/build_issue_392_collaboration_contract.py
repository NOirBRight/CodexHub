#!/usr/bin/env python3
"""Build and validate the frozen Beta4 Collaboration runtime contract.

The artifact is deliberately bounded.  It records source- and runtime-derived
wire shapes, but never stores prompts, credentials, paths, task identifiers,
call identifiers, or message content.
"""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


_SRC_PYTHON = Path(__file__).resolve().parents[1] / "src-python"
if str(_SRC_PYTHON) not in sys.path:
    sys.path.insert(0, str(_SRC_PYTHON))

from collaboration_runtime_contract import (  # noqa: E402
    CollaborationContractError as _ProductionContractError,
    classify_collaboration_request as _classify_production_request,
    classify_collaboration_tools as _classify_production_tools,
)


SCHEMA = "codexhub.issue392.collaboration-runtime-contract.v1"
DEFAULT_OUTPUT = Path("docs/evidence/issue-392/collaboration-runtime-contract.json")
OBSERVATION_SCHEMA = "codexhub.issue392.collaboration-runtime-observations.v1"
DEFAULT_OBSERVATIONS = Path(
    "docs/evidence/issue-392/collaboration-runtime-observations.json"
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CAPTURE_DATE = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}\Z")

V1 = "collaboration_v1"
V2 = "collaboration_v2"
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

def _object_schema(
    properties: Mapping[str, Any], required: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


STRING = {"type": "string"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}
NULLABLE_STRING = {"type": ["string", "null"]}
ENCRYPTED_STRING = {"type": "string", "encrypted": True}

COLLAB_INPUT_ITEM_SCHEMA = _object_schema(
    {
        "audio_url": STRING,
        "image_url": STRING,
        "name": STRING,
        "path": STRING,
        "text": STRING,
        "type": STRING,
    }
)

# Descriptions are deliberately excluded because user roles and runtime wording
# can change them without changing the wire contract.  Types, encryption flags,
# required fields, nesting, and additionalProperties are exact normalized
# captures from both frozen clients.
EXPECTED_PARAMETER_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    V1: {
        "close_agent": _object_schema({"target": STRING}, ["target"]),
        "resume_agent": _object_schema({"id": STRING}, ["id"]),
        "send_input": _object_schema(
            {
                "interrupt": BOOLEAN,
                "items": {"type": "array", "items": COLLAB_INPUT_ITEM_SCHEMA},
                "message": STRING,
                "target": STRING,
            },
            ["target"],
        ),
        "spawn_agent": _object_schema(
            {
                "agent_type": STRING,
                "fork_context": BOOLEAN,
                "items": {"type": "array", "items": COLLAB_INPUT_ITEM_SCHEMA},
                "message": STRING,
                "model": STRING,
                "reasoning_effort": STRING,
                "service_tier": STRING,
            }
        ),
        "wait_agent": _object_schema(
            {
                "targets": {"type": "array", "items": STRING},
                "timeout_ms": NUMBER,
            },
            ["targets"],
        ),
    },
    V2: {
        "followup_task": _object_schema(
            {"message": ENCRYPTED_STRING, "target": STRING},
            ["target", "message"],
        ),
        "interrupt_agent": _object_schema({"target": STRING}, ["target"]),
        "list_agents": _object_schema({"path_prefix": STRING}),
        "send_message": _object_schema(
            {"message": ENCRYPTED_STRING, "target": STRING},
            ["target", "message"],
        ),
        "spawn_agent": _object_schema(
            {
                "agent_type": STRING,
                "fork_turns": STRING,
                "message": ENCRYPTED_STRING,
                "model": STRING,
                "reasoning_effort": STRING,
                "task_name": STRING,
            },
            ["task_name", "message"],
        ),
        "wait_agent": _object_schema({"timeout_ms": NUMBER}),
    },
}


def _argument_sets(
    schema: Mapping[str, Any],
) -> tuple[frozenset[str], frozenset[str]]:
    properties = frozenset(schema["properties"])
    required = frozenset(schema.get("required", []))
    return required, properties - required


EXPECTED_ARGUMENTS: dict[
    str, dict[str, tuple[frozenset[str], frozenset[str]]]
] = {
    version: {
        name: _argument_sets(schema) for name, schema in tool_schemas.items()
    }
    for version, tool_schemas in EXPECTED_PARAMETER_SCHEMAS.items()
}


AGENT_STATUS_SCHEMA = {
    "oneOf": [
        {
            "type": "string",
            "enum": ["pending_init", "running", "interrupted", "shutdown", "not_found"],
        },
        _object_schema({"completed": NULLABLE_STRING}, ["completed"]),
        _object_schema({"errored": STRING}, ["errored"]),
    ]
}

EXPECTED_OUTPUT_SCHEMAS: dict[str, dict[str, dict[str, Any] | None]] = {
    V1: {
        "close_agent": _object_schema(
            {"previous_status": AGENT_STATUS_SCHEMA}, ["previous_status"]
        ),
        "resume_agent": _object_schema({"status": AGENT_STATUS_SCHEMA}, ["status"]),
        "send_input": _object_schema({"submission_id": STRING}, ["submission_id"]),
        "spawn_agent": _object_schema(
            {"agent_id": STRING, "nickname": NULLABLE_STRING},
            ["agent_id", "nickname"],
        ),
        "wait_agent": _object_schema(
            {
                "status": {"type": "object", "additionalProperties": AGENT_STATUS_SCHEMA},
                "timed_out": BOOLEAN,
            },
            ["status", "timed_out"],
        ),
    },
    V2: {
        "followup_task": None,
        "interrupt_agent": _object_schema(
            {"previous_status": AGENT_STATUS_SCHEMA}, ["previous_status"]
        ),
        "list_agents": _object_schema(
            {
                "agents": {
                    "type": "array",
                    "items": _object_schema(
                        {"agent_name": STRING, "agent_status": AGENT_STATUS_SCHEMA},
                        ["agent_name", "agent_status"],
                    ),
                }
            },
            ["agents"],
        ),
        "send_message": None,
        "spawn_agent": _object_schema({"task_name": STRING}, ["task_name"]),
        "wait_agent": _object_schema(
            {"message": STRING, "timed_out": BOOLEAN}, ["message", "timed_out"]
        ),
    },
}

SOURCE_FILES = {
    "multi_agent_tool_schemas": "codex-rs/core/src/tools/handlers/multi_agents_spec.rs",
    "multi_agent_output_serialization": "codex-rs/core/src/tools/handlers/multi_agents_common.rs",
    "v2_spawn_handler": "codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs",
    "v2_message_handler": "codex-rs/core/src/tools/handlers/multi_agents_v2/message_tool.rs",
    "v2_wait_handler": "codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs",
    "v2_interrupt_handler": "codex-rs/core/src/tools/handlers/multi_agents_v2/interrupt_agent.rs",
    "v2_list_handler": "codex-rs/core/src/tools/handlers/multi_agents_v2/list_agents.rs",
    "tool_planning_and_namespace_override": "codex-rs/core/src/tools/spec_plan.rs",
    "version_selection_and_namespace_default": "codex-rs/core/src/config/mod.rs",
    "responses_request_and_tool_choice": "codex-rs/core/src/client.rs",
    "responses_stream_parser": "codex-rs/codex-api/src/sse/responses.rs",
    "responses_items": "codex-rs/protocol/src/models.rs",
    "agent_message_and_rollout": "codex-rs/protocol/src/protocol.rs",
    "desktop_thread_items": "codex-rs/app-server-protocol/src/protocol/v2/item.rs",
    "desktop_event_mapping": "codex-rs/app-server-protocol/src/protocol/event_mapping.rs",
}

CLI_SOURCE_BLOBS = {
    "multi_agent_tool_schemas": "2eb82e175b454db0fc8fb8bba61b37d07ab69d30",
    "multi_agent_output_serialization": "9bb7f76f9de0d799766d050c5811e33d3a7635ea",
    "v2_spawn_handler": "67f8057e435b8522a82825f055c75ed420401975",
    "v2_message_handler": "62defe65c4114ffcdc661b98836d9964f09a7c64",
    "v2_wait_handler": "4e2eced26037d4c876a8b9fcd28912fb4344953b",
    "v2_interrupt_handler": "a418dec4a86550715080b95a3191ce3c72836e8f",
    "v2_list_handler": "99e54e1ce91cbd0f182f52fd8ff16f9268f96233",
    "tool_planning_and_namespace_override": "5ff9e392f3ebec0a510efd11305752133944ceda",
    "version_selection_and_namespace_default": "216af994bc03e136c4797453525df473f579adf5",
    "responses_request_and_tool_choice": "b13242062022d9d37302a060dd493977d9c0c1f1",
    "responses_stream_parser": "441d7bb3bfa743f2fc3fe089cfdb88ddec887aac",
    "responses_items": "51e22d10fd57af6bffd7a11b60dc2d422a7ad607",
    "agent_message_and_rollout": "c692d040d292cf223777cae42dd577b03e85aab5",
    "desktop_thread_items": "f3a3a65d20516ac9dc99ef0c981eaf399e151cfa",
    "desktop_event_mapping": "b32b15272ba4791db5a253ecc50555bf29b4ef14",
}

DESKTOP_SOURCE_BLOBS = {
    "multi_agent_tool_schemas": "757e111cc2d060f84b2618896426001225bba4ef",
    "multi_agent_output_serialization": "f8569c441ed24a01760a51674687787014dfde28",
    "v2_spawn_handler": "a6b5c46eec06931354fdc3109909fd574865611d",
    "v2_message_handler": "eb1790480eeeb5d524cfb132846812ce073c674f",
    "v2_wait_handler": "4e2eced26037d4c876a8b9fcd28912fb4344953b",
    "v2_interrupt_handler": "a418dec4a86550715080b95a3191ce3c72836e8f",
    "v2_list_handler": "99e54e1ce91cbd0f182f52fd8ff16f9268f96233",
    "tool_planning_and_namespace_override": "1047190cc37f34ec4cfcae348af170da2bacc4c7",
    "version_selection_and_namespace_default": "844184314f2fa3a2fe2f8238e8d351aaa1050a5e",
    "responses_request_and_tool_choice": "d6b25864dfa4620682d163cf9b65a4747d8fcfa9",
    "responses_stream_parser": "44ed78737ba4ecf9e777c9d38199005ea4e2c2cc",
    "responses_items": "2c2a6d6b7aa49eee6309e5a9ba9e7587a1047d4e",
    "agent_message_and_rollout": "93313c63c2dd185e02d20fff9eff31d43b461277",
    "desktop_thread_items": "a98ec3f3b2e19f4fef57ae7a5451a54dc471fb9b",
    "desktop_event_mapping": "b32b15272ba4791db5a253ecc50555bf29b4ef14",
}


class ContractValidationError(ValueError):
    """Stable bounded failure; values from a request are never included."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractValidationError(code)


def _argument_contract(version: str) -> dict[str, Any]:
    return {
        name: {
            "required": sorted(required),
            "optional": sorted(optional),
            "additional_properties": False,
            "normalized_parameter_schema": copy.deepcopy(
                EXPECTED_PARAMETER_SCHEMAS[version][name]
            ),
            "request_output_schema_field": "absent",
            "client_result_schema": copy.deepcopy(EXPECTED_OUTPUT_SCHEMAS[version][name]),
        }
        for name, (required, optional) in EXPECTED_ARGUMENTS[version].items()
    }


def _runtime(
    *,
    runtime_observation: Mapping[str, Any],
    desktop_app: bool,
    scenario_names: Sequence[str],
    home_bindings: Sequence[str],
) -> dict[str, Any]:
    return {
        "client": runtime_observation["client"],
        "client_version": runtime_observation["client_version"],
        "runtime_version": runtime_observation["runtime_version"],
        "binary_sha256": runtime_observation["binary_sha256"],
        "source_tag": runtime_observation["source_tag"],
        "source_commit": runtime_observation["source_commit"],
        "source_files": copy.deepcopy(runtime_observation["source_files"]),
        "observations": {
            "v1_namespace_declaration": "observed",
            "v2_namespace_declaration": "observed",
            "tool_choice_auto": "observed",
            "v2_function_call_and_output": "observed",
            "v2_agent_message_input": "observed",
            "v2_same_home_restart_readback": "observed",
            "v2_rollout_version_metadata": "observed",
            "desktop_thread_items_and_notifications": (
                "observed" if desktop_app else "not_applicable"
            ),
        },
        "observation_scenarios": list(scenario_names),
        "isolated_home_bindings_sha256": list(home_bindings),
    }


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _capture_run_binding(payload: Mapping[str, Any]) -> str:
    clone = copy.deepcopy(dict(payload))
    clone.pop("capture_run_binding_sha256", None)
    return _canonical_digest(clone)


def _expected_runtime_observation(
    *,
    client: str,
    client_version: str,
    runtime_version: str,
    binary_sha256: str,
    source_tag: str,
    source_commit: str,
    source_blobs: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "client": client,
        "client_version": client_version,
        "runtime_version": runtime_version,
        "version_output": f"codex-cli {runtime_version}",
        "binary_sha256": binary_sha256,
        "source_tag": source_tag,
        "source_commit": source_commit,
        "source_files": {
            key: {"path": SOURCE_FILES[key], "git_blob": source_blobs[key]}
            for key in SOURCE_FILES
        },
    }


def _expected_observed_declaration(version: str) -> dict[str, Any]:
    namespace = V1_NAMESPACE if version == V1 else V2_NAMESPACE
    children = [
        {
            "type": "function",
            "name": name,
            "description_type": "string",
            "strict": False,
            "fields": ["description", "name", "parameters", "strict", "type"],
            "parameters": _normalize_schema(schema),
        }
        for name, schema in EXPECTED_PARAMETER_SCHEMAS[version].items()
    ]
    children.sort(key=lambda value: value["name"])
    return {
        "type": "namespace",
        "name": namespace,
        "description_type": "string",
        "fields": ["description", "name", "tools", "type"],
        "children": children,
    }


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], code: str
) -> None:
    _require(set(value) == expected, code)


def _require_digest(value: Any, code: str) -> None:
    _require(isinstance(value, str) and _DIGEST.fullmatch(value) is not None, code)


def _validate_safe_request(value: Any) -> None:
    _require(isinstance(value, Mapping), "observation_request_invalid")
    _require_exact_fields(
        value,
        {
            "fields",
            "field_types",
            "tool_choice",
            "multi_agent_version_locations",
            "input_type_order",
            "collaboration_items",
        },
        "observation_request_fields_invalid",
    )
    _require(value.get("tool_choice") == "auto", "observation_tool_choice_invalid")
    _require(
        value.get("multi_agent_version_locations") == [],
        "observation_version_location_invalid",
    )
    _require(isinstance(value.get("fields"), list), "observation_request_fields_invalid")
    _require(
        isinstance(value.get("field_types"), Mapping),
        "observation_request_field_types_invalid",
    )
    _require(
        value["fields"] == sorted(value["field_types"]),
        "observation_request_field_type_keys_invalid",
    )
    _require(
        isinstance(value.get("input_type_order"), list),
        "observation_input_order_invalid",
    )
    items = value.get("collaboration_items")
    _require(isinstance(items, list), "observation_collaboration_items_invalid")
    for item in items:
        _require(isinstance(item, Mapping), "observation_collaboration_item_invalid")
        item_type = item.get("type")
        if item_type == "function_call":
            _require_exact_fields(
                item,
                {
                    "type",
                    "fields",
                    "field_types",
                    "name",
                    "namespace",
                    "arguments_json_type",
                    "argument_keys",
                },
                "observation_function_call_fields_invalid",
            )
            _require(
                item.get("namespace") == V2_NAMESPACE,
                "observation_function_call_namespace_invalid",
            )
            _require(
                item.get("name") in V2_TOOLS,
                "observation_function_call_name_invalid",
            )
            _require(
                item.get("arguments_json_type") == "object",
                "observation_function_arguments_invalid",
            )
            required_types = {
                "type": "string",
                "id": "string",
                "name": "string",
                "namespace": "string",
                "arguments": "string",
                "call_id": "string",
            }
            _require(
                all(item["field_types"].get(key) == value for key, value in required_types.items()),
                "observation_function_call_identity_invalid",
            )
        elif item_type == "function_call_output":
            _require_exact_fields(
                item,
                {
                    "type",
                    "fields",
                    "field_types",
                    "output_wire_type",
                    "decoded_output_type",
                    "decoded_output_keys",
                },
                "observation_function_output_fields_invalid",
            )
            _require(
                item.get("output_wire_type") == "string",
                "observation_function_output_wire_invalid",
            )
            required_types = {
                "type": "string",
                "id": "string",
                "call_id": "string",
                "output": "string",
            }
            _require(
                all(item["field_types"].get(key) == value for key, value in required_types.items()),
                "observation_function_output_identity_invalid",
            )
        elif item_type == "agent_message":
            _require_exact_fields(
                item,
                {"type", "fields", "field_types", "content_variants"},
                "observation_agent_message_fields_invalid",
            )
            variants = item.get("content_variants")
            required_types = {
                "type": "string",
                "id": "string",
                "author": "string",
                "recipient": "string",
                "content": "array",
            }
            _require(
                all(item["field_types"].get(key) == value for key, value in required_types.items()),
                "observation_agent_message_identity_invalid",
            )
            _require(isinstance(variants, list), "observation_agent_message_invalid")
            for variant in variants:
                _require(
                    isinstance(variant, Mapping),
                    "observation_agent_message_variant_invalid",
                )
                _require_exact_fields(
                    variant,
                    {"type", "fields", "field_types"},
                    "observation_agent_message_variant_fields_invalid",
                )
                _require(
                    variant.get("type") in {"input_text", "encrypted_content"},
                    "observation_agent_message_variant_invalid",
                )
                _require(
                    variant.get("fields") == sorted(variant.get("field_types", {})),
                    "observation_agent_message_variant_type_keys_invalid",
                )
                content_field = (
                    "text" if variant.get("type") == "input_text" else "encrypted_content"
                )
                _require(
                    variant["field_types"].get("type") == "string"
                    and variant["field_types"].get(content_field) == "string",
                    "observation_agent_message_variant_content_invalid",
                )
        else:
            raise ContractValidationError("observation_collaboration_item_type_invalid")
        _require(
            item.get("fields") == sorted(item.get("field_types", {})),
            "observation_collaboration_item_field_type_keys_invalid",
        )


def _collaboration_signature(request: Mapping[str, Any]) -> list[Any]:
    result: list[Any] = []
    for item in request["collaboration_items"]:
        item_type = item["type"]
        if item_type == "function_call":
            result.append([item_type, item["name"]])
        elif item_type == "function_call_output":
            result.append([item_type, item["decoded_output_keys"]])
        else:
            result.append(
                [
                    item_type,
                    [variant["type"] for variant in item["content_variants"]],
                ]
            )
    return result


def _validate_event_envelope(value: Any) -> None:
    _require(isinstance(value, Mapping), "observation_event_envelope_invalid")
    base_fields = {"type", "fields", "field_types"}
    optional_groups = {
        "item_fields": {"item_fields", "item_field_types"},
        "part_fields": {"part_fields", "part_field_types"},
        "response_fields": {"response_fields", "response_field_types"},
        "response_error_fields": {
            "response_error_fields",
            "response_error_field_types",
        },
        "response_incomplete_details_fields": {
            "response_incomplete_details_fields",
            "response_incomplete_details_field_types",
        },
    }
    allowed = base_fields | {
        child for group in optional_groups.values() for child in group
    } | {
        "item_type",
        "item_status",
        "item_name",
        "item_namespace",
        "part_type",
        "response_status",
        "response_output_item_types",
    }
    _require(base_fields <= set(value) <= allowed, "observation_event_envelope_fields_invalid")
    event_type = value.get("type")
    _require(
        event_type
        in {
            "response.created",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
            "response.incomplete",
            "response.failed",
        },
        "observation_event_type_invalid",
    )
    _require(
        value.get("fields") == sorted(value.get("field_types", {})),
        "observation_event_field_type_keys_invalid",
    )
    expected_event_field_types = {
        "response.created": {"response": "object", "type": "string"},
        "response.output_item.added": {
            "item": "object",
            "output_index": "number",
            "type": "string",
        },
        "response.content_part.added": {
            "content_index": "number",
            "item_id": "string",
            "output_index": "number",
            "part": "object",
            "type": "string",
        },
        "response.output_text.delta": {
            "content_index": "number",
            "delta": "string",
            "item_id": "string",
            "output_index": "number",
            "type": "string",
        },
        "response.output_text.done": {
            "content_index": "number",
            "item_id": "string",
            "output_index": "number",
            "text": "string",
            "type": "string",
        },
        "response.function_call_arguments.delta": {
            "delta": "string",
            "item_id": "string",
            "output_index": "number",
            "type": "string",
        },
        "response.function_call_arguments.done": {
            "arguments": "string",
            "item_id": "string",
            "output_index": "number",
            "type": "string",
        },
        "response.output_item.done": {
            "item": "object",
            "output_index": "number",
            "type": "string",
        },
        "response.completed": {"response": "object", "type": "string"},
        "response.incomplete": {"response": "object", "type": "string"},
        "response.failed": {"response": "object", "type": "string"},
    }
    _require(
        value.get("field_types") == expected_event_field_types[event_type],
        "observation_event_field_types_invalid",
    )
    for field_name, group in optional_groups.items():
        present = group & set(value)
        _require(not present or present == group, "observation_event_nested_pair_invalid")
        if present:
            type_field = next(name for name in group if name.endswith("_field_types"))
            _require(
                value[field_name] == sorted(value[type_field]),
                "observation_event_nested_type_keys_invalid",
            )
    if "item_type" in value:
        _require(
            value["item_type"] in {"function_call", "message"},
            "observation_event_item_type_invalid",
        )
    if "item_status" in value:
        _require(
            value["item_status"] in {"in_progress", "completed"},
            "observation_event_item_status_invalid",
        )
    if "item_name" in value:
        _require(value["item_name"] in V2_TOOLS, "observation_event_item_name_invalid")
    if "item_namespace" in value:
        _require(
            value["item_namespace"] == V2_NAMESPACE,
            "observation_event_item_namespace_invalid",
        )
    if "part_type" in value:
        _require(value["part_type"] == "output_text", "observation_event_part_type_invalid")
    if "response_status" in value:
        _require(
            value["response_status"] in {"in_progress", "completed", "incomplete"},
            "observation_event_response_status_invalid",
        )
    if "response_output_item_types" in value:
        _require(
            isinstance(value["response_output_item_types"], list)
            and all(
                item_type in {"function_call", "message"}
                for item_type in value["response_output_item_types"]
            ),
            "observation_event_response_output_invalid",
        )
    if value.get("item_type") == "function_call":
        item_types = value.get("item_field_types")
        _require(isinstance(item_types, Mapping), "observation_event_function_item_invalid")
        required_types = {
            "type": "string",
            "id": "string",
            "status": "string",
            "name": "string",
            "namespace": "string",
            "arguments": "string",
            "call_id": "string",
        }
        _require(
            dict(item_types) == required_types,
            "observation_event_function_identity_invalid",
        )
    elif value.get("item_type") == "message":
        _require(
            value.get("item_field_types")
            == {
                "content": "array",
                "id": "string",
                "role": "string",
                "type": "string",
            },
            "observation_event_message_item_invalid",
        )
    if "part_field_types" in value:
        _require(
            value["part_field_types"]
            == {"annotations": "array", "text": "string", "type": "string"},
            "observation_event_part_fields_invalid",
        )
    expected_response_types = {
        "response.created": {
            "id": "string",
            "model": "string",
            "object": "string",
            "output": "array",
            "status": "string",
        },
        "response.completed": {
            "id": "string",
            "model": "string",
            "object": "string",
            "output": "array",
            "status": "string",
            "usage": "object",
        },
        "response.incomplete": {
            "error": "null",
            "id": "string",
            "incomplete_details": "object",
            "object": "string",
            "status": "string",
        },
        "response.failed": {"error": "object", "id": "string"},
    }
    if event_type in expected_response_types:
        _require(
            value.get("response_field_types") == expected_response_types[event_type],
            "observation_event_response_fields_invalid",
        )
    if event_type == "response.failed":
        _require(
            value.get("response_error_field_types")
            == {"code": "string", "message": "string"},
            "observation_event_response_error_invalid",
        )
    if event_type == "response.incomplete":
        _require(
            value.get("response_incomplete_details_field_types")
            == {"reason": "string"},
            "observation_event_incomplete_details_invalid",
        )


def _validate_event_sequences(value: Any) -> None:
    _require(isinstance(value, list) and value, "observation_event_sequences_invalid")
    request_indices: list[int] = []
    for sequence in value:
        _require(isinstance(sequence, Mapping), "observation_event_sequence_invalid")
        _require_exact_fields(
            sequence,
            {"request_index", "events"},
            "observation_event_sequence_fields_invalid",
        )
        request_index = sequence.get("request_index")
        _require(
            isinstance(request_index, int)
            and not isinstance(request_index, bool)
            and request_index >= 1,
            "observation_event_sequence_index_invalid",
        )
        request_indices.append(request_index)
        events = sequence.get("events")
        _require(isinstance(events, list) and events, "observation_event_sequence_empty")
        for event in events:
            _validate_event_envelope(event)
    _require(
        sorted(request_indices) == list(range(1, len(request_indices) + 1)),
        "observation_event_sequence_indices_invalid",
    )


def _validate_v2_phase_requests(
    phases: Any, *, includes_restart: bool
) -> None:
    _require(isinstance(phases, Mapping), "observation_phase_requests_invalid")
    expected = {
        "root_initial",
        "root_after_spawn",
        "child_initial",
        "root_after_wait",
    }
    if includes_restart:
        expected.add("restart_replay")
    _require(set(phases) == expected, "observation_phase_set_invalid")
    for request in phases.values():
        _validate_safe_request(request)
    signatures = {
        phase: _collaboration_signature(request) for phase, request in phases.items()
    }
    _require(signatures["root_initial"] == [], "observation_root_initial_invalid")
    _require(
        signatures["root_after_spawn"]
        == [
            ["function_call", "spawn_agent"],
            ["function_call_output", ["task_name"]],
        ],
        "observation_after_spawn_invalid",
    )
    _require(
        signatures["child_initial"]
        == [["agent_message", ["input_text", "encrypted_content"]]],
        "observation_child_initial_invalid",
    )
    expected_after_wait = [
        ["function_call", "spawn_agent"],
        ["function_call_output", ["task_name"]],
        ["function_call", "wait_agent"],
        ["function_call_output", ["message", "timed_out"]],
        ["agent_message", ["input_text"]],
    ]
    _require(
        signatures["root_after_wait"] == expected_after_wait,
        "observation_after_wait_invalid",
    )
    if includes_restart:
        _require(
            signatures["restart_replay"] == expected_after_wait,
            "observation_restart_replay_invalid",
        )


def _require_all_true(value: Any, code: str) -> None:
    _require(isinstance(value, Mapping) and value, code)
    _require(all(child is True for child in value.values()), code)


def _validate_rollout_readback(value: Any) -> None:
    _require(isinstance(value, list) and value, "observation_rollout_invalid")
    roles = {entry.get("role") for entry in value if isinstance(entry, Mapping)}
    _require(roles == {"root", "child"}, "observation_rollout_roles_invalid")
    for entry in value:
        _require(isinstance(entry, Mapping), "observation_rollout_entry_invalid")
        _require_exact_fields(
            entry,
            {
                "role",
                "relevant_record_type_order",
                "metadata_records",
                "collaboration_items",
            },
            "observation_rollout_fields_invalid",
        )
        metadata = entry.get("metadata_records")
        _require(isinstance(metadata, list) and metadata, "observation_metadata_invalid")
        record_order = entry.get("relevant_record_type_order")
        _require(
            isinstance(record_order, list)
            and all(
                record_type in {"session_meta", "turn_context", "response_item"}
                for record_type in record_order
            ),
            "observation_rollout_record_order_invalid",
        )
        for record in metadata:
            _require(isinstance(record, Mapping), "observation_metadata_record_invalid")
            _require_exact_fields(
                record,
                {
                    "record_type",
                    "record_fields",
                    "record_field_types",
                    "payload_fields",
                    "payload_field_types",
                    "multi_agent_version",
                },
                "observation_metadata_record_fields_invalid",
            )
            _require(
                record.get("record_fields")
                == sorted(record.get("record_field_types", {})),
                "observation_metadata_record_type_keys_invalid",
            )
            _require(
                record.get("payload_fields")
                == sorted(record.get("payload_field_types", {})),
                "observation_metadata_payload_type_keys_invalid",
            )
        versions = [
            record.get("multi_agent_version")
            for record in metadata
            if isinstance(record, Mapping)
            and record.get("record_type") in {"session_meta", "turn_context"}
        ]
        _require(versions and set(versions) == {"v2"}, "observation_rollout_version_invalid")
        for item in entry.get("collaboration_items", []):
            _validate_safe_request(
                {
                    "fields": [],
                    "field_types": {},
                    "tool_choice": "auto",
                    "multi_agent_version_locations": [],
                    "input_type_order": [],
                    "collaboration_items": [item],
                }
            )


def _validate_thread_structure(value: Any) -> None:
    _require(isinstance(value, Mapping), "observation_thread_structure_invalid")
    _require_exact_fields(
        value,
        {
            "fields",
            "field_types",
            "status_fields",
            "status_field_types",
            "turns",
        },
        "observation_thread_structure_fields_invalid",
    )
    turns = value.get("turns")
    _require(
        value.get("fields") == sorted(value.get("field_types", {})),
        "observation_thread_structure_type_keys_invalid",
    )
    _require(
        value.get("status_fields") == sorted(value.get("status_field_types", {})),
        "observation_thread_status_type_keys_invalid",
    )
    _require(isinstance(turns, list) and turns, "observation_thread_turns_invalid")
    for turn in turns:
        _require(isinstance(turn, Mapping), "observation_thread_turn_invalid")
        _require_exact_fields(
            turn,
            {"fields", "field_types", "status", "items"},
            "observation_thread_turn_fields_invalid",
        )
        _require(
            turn.get("fields") == sorted(turn.get("field_types", {})),
            "observation_thread_turn_type_keys_invalid",
        )
        items = turn.get("items")
        _require(isinstance(items, list), "observation_thread_items_invalid")
        for item in items:
            _require(isinstance(item, Mapping), "observation_thread_item_invalid")
            _require(
                {"type", "fields", "field_types"}.issubset(item),
                "observation_thread_item_fields_invalid",
            )
            _require(
                set(item).issubset(
                    {"type", "fields", "field_types", "kind", "status", "tool", "phase"}
                ),
                "observation_thread_item_fields_invalid",
            )
            _require(
                item.get("fields") == sorted(item.get("field_types", {})),
                "observation_thread_item_type_keys_invalid",
            )


def validate_runtime_observations(payload: Mapping[str, Any]) -> None:
    _require(isinstance(payload, Mapping), "observations_invalid")
    _require_exact_fields(
        payload,
        {
            "schema",
            "candidate_revision",
            "captured_on",
            "capture_run_binding_sha256",
            "controls",
            "runtimes",
            "scenarios",
        },
        "observations_fields_invalid",
    )
    _require(payload.get("schema") == OBSERVATION_SCHEMA, "observations_schema_invalid")
    _require(
        payload.get("candidate_revision")
        == "be10f62f44b22fa8c84510238250ae11fb3ecab4",
        "observations_candidate_invalid",
    )
    captured_on = payload.get("captured_on")
    _require(
        isinstance(captured_on, str) and _CAPTURE_DATE.fullmatch(captured_on) is not None,
        "observations_capture_date_invalid",
    )
    binding = payload.get("capture_run_binding_sha256")
    _require_digest(binding, "observations_binding_invalid")
    _require(binding == _capture_run_binding(payload), "observations_binding_mismatch")
    expected_controls = {
        "explicit_runtime_capture": True,
        "fresh_empty_home_per_scenario": True,
        "workspace_under_isolated_home": True,
        "protocol_upstream_loopback": True,
        "plugin_services_disabled": ["plugins", "remote_plugin", "plugin_sharing"],
        "sensitive_environment_credentials_removed": True,
        "external_config_environment_removed": ["CODEX_CONFIG"],
        "xdg_roots_under_isolated_home": True,
        "existing_user_home_read": False,
        "existing_user_task_read": False,
        "known_crash_task_read": False,
        "raw_content_or_credentials_retained": False,
        "raw_paths_or_opaque_identifiers_retained": False,
    }
    _require(payload.get("controls") == expected_controls, "observations_controls_invalid")
    runtimes = payload.get("runtimes")
    _require(isinstance(runtimes, Mapping), "observations_runtimes_invalid")
    expected_runtimes = {
        "codex_cli": _expected_runtime_observation(
            client="codex_cli",
            client_version="0.146.1",
            runtime_version="0.146.1",
            binary_sha256="ae9d865f3d346a1a2a60c4e84775622d74e3e7ef53e0dede9c68b81eab306cca",
            source_tag="rust-v0.146.1",
            source_commit="79b4f03d35962b005b007a015113b38930711665",
            source_blobs=CLI_SOURCE_BLOBS,
        ),
        "codex_desktop": _expected_runtime_observation(
            client="codex_desktop",
            client_version="26.803.5235.0",
            runtime_version="0.147.0-alpha.6.5",
            binary_sha256="fb5c760e14cf8fe86e12e49e8a3e7f237af06082d6b9fe1e411e463b7229c916",
            source_tag="rust-v0.147.0-alpha.6.5",
            source_commit="618b8e9111da9f57fe380b09d0f6516e3f343536",
            source_blobs=DESKTOP_SOURCE_BLOBS,
        ),
    }
    _require(dict(runtimes) == expected_runtimes, "observations_runtime_inputs_invalid")

    scenarios = payload.get("scenarios")
    _require(isinstance(scenarios, Mapping), "observations_scenarios_invalid")
    scenario_contract = {
        "cli_v1_request": ("codex_cli", 1, 1),
        "desktop_v1_request": ("codex_desktop", 1, 1),
        "cli_v2_lifecycle": ("codex_cli", 5, 2),
        "desktop_v2_lifecycle": ("codex_desktop", 5, 2),
        "cli_terminal_incomplete": ("codex_cli", 1, 1),
        "cli_terminal_failed": ("codex_cli", 1, 1),
        "cli_terminal_truncated": ("codex_cli", 1, 1),
        "desktop_terminal_incomplete": ("codex_desktop", 1, 1),
        "desktop_terminal_failed": ("codex_desktop", 1, 1),
        "desktop_terminal_truncated": ("codex_desktop", 1, 1),
        "desktop_app_v2_lifecycle": ("codex_desktop", 4, 2),
    }
    _require(set(scenarios) == set(scenario_contract), "observations_scenario_set_invalid")
    home_bindings: list[str] = []
    for name, (client, request_count, process_runs) in scenario_contract.items():
        scenario = scenarios[name]
        _require(isinstance(scenario, Mapping), "observation_scenario_invalid")
        _require_exact_fields(
            scenario,
            {
                "client",
                "home_binding_sha256",
                "fresh_empty_home_before_marker",
                "workspace_created_under_home",
                "loopback_request_count",
                "process_runs",
                "observed",
            },
            "observation_scenario_fields_invalid",
        )
        _require(scenario.get("client") == client, "observation_scenario_client_invalid")
        _require_digest(scenario.get("home_binding_sha256"), "observation_home_binding_invalid")
        home_bindings.append(scenario["home_binding_sha256"])
        _require(
            scenario.get("fresh_empty_home_before_marker") is True,
            "observation_home_not_fresh",
        )
        _require(
            scenario.get("workspace_created_under_home") is True,
            "observation_workspace_scope_invalid",
        )
        _require(
            scenario.get("loopback_request_count") == request_count,
            "observation_request_count_invalid",
        )
        _require(
            scenario.get("process_runs") == process_runs,
            "observation_process_runs_invalid",
        )
    _require(len(set(home_bindings)) == len(home_bindings), "observation_home_binding_duplicate")

    for name in ("cli_v1_request", "desktop_v1_request"):
        observed = scenarios[name]["observed"]
        _require(isinstance(observed, Mapping), "observation_v1_invalid")
        _require_exact_fields(
            observed, {"request", "declaration"}, "observation_v1_fields_invalid"
        )
        _validate_safe_request(observed["request"])
        _require(
            observed["request"]["collaboration_items"] == [],
            "observation_v1_history_invalid",
        )
        _require(
            observed["declaration"] == _expected_observed_declaration(V1),
            "observation_v1_declaration_invalid",
        )

    for name in ("cli_v2_lifecycle", "desktop_v2_lifecycle"):
        observed = scenarios[name]["observed"]
        _require(isinstance(observed, Mapping), "observation_v2_invalid")
        _require_exact_fields(
            observed,
            {
                "declaration",
                "request_arrival_order",
                "requests_by_phase",
                "served_function_call_event_order",
                "served_function_names",
                "served_event_sequences",
                "identity_relationships",
                "rollout_readback",
            },
            "observation_v2_fields_invalid",
        )
        _require(
            observed["declaration"] == _expected_observed_declaration(V2),
            "observation_v2_declaration_invalid",
        )
        _validate_v2_phase_requests(observed["requests_by_phase"], includes_restart=True)
        _require(
            sorted(observed["request_arrival_order"])
            == sorted(observed["requests_by_phase"]),
            "observation_request_arrival_invalid",
        )
        _require(
            observed["served_function_call_event_order"]
            == [
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
            "observation_stream_order_invalid",
        )
        _require(
            observed["served_function_names"] == ["spawn_agent", "wait_agent"],
            "observation_served_functions_invalid",
        )
        _validate_event_sequences(observed["served_event_sequences"])
        _require(
            len(observed["served_event_sequences"])
            == scenarios[name]["loopback_request_count"],
            "observation_event_sequence_count_invalid",
        )
        function_sequences = [
            sequence["events"]
            for sequence in observed["served_event_sequences"]
            if any(
                event["type"] == "response.function_call_arguments.delta"
                for event in sequence["events"]
            )
        ]
        _require(len(function_sequences) == 2, "observation_function_stream_count_invalid")
        expected_function_stream = [
            "response.created",
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
        ]
        _require(
            all(
                [event["type"] for event in sequence] == expected_function_stream
                for sequence in function_sequences
            ),
            "observation_function_stream_invalid",
        )
        _require_all_true(
            observed["identity_relationships"],
            "observation_identity_relationship_invalid",
        )
        _validate_rollout_readback(observed["rollout_readback"])

    for client_prefix in ("cli", "desktop"):
        for terminal in ("incomplete", "failed", "truncated"):
            name = f"{client_prefix}_terminal_{terminal}"
            observed = scenarios[name]["observed"]
            _require(isinstance(observed, Mapping), "observation_terminal_control_invalid")
            _require_exact_fields(
                observed,
                {
                    "terminal_control",
                    "client_disposition",
                    "request",
                    "declaration",
                    "served_event_envelopes",
                    "terminal_event_present",
                    "completed_event_present",
                },
                "observation_terminal_control_fields_invalid",
            )
            _require(
                observed["terminal_control"] == terminal,
                "observation_terminal_control_kind_invalid",
            )
            _require(
                observed["client_disposition"] == "rejected_nonzero_exit",
                "observation_terminal_disposition_invalid",
            )
            _validate_safe_request(observed["request"])
            _require(
                observed["declaration"] == _expected_observed_declaration(V2),
                "observation_terminal_declaration_invalid",
            )
            events = observed["served_event_envelopes"]
            _require(isinstance(events, list) and events, "observation_terminal_events_invalid")
            for event in events:
                _validate_event_envelope(event)
            event_types = [event["type"] for event in events]
            expected_terminal_type = f"response.{terminal}"
            if terminal == "truncated":
                _require(
                    observed["terminal_event_present"] is False
                    and not any(
                        event_type
                        in {"response.completed", "response.incomplete", "response.failed"}
                        for event_type in event_types
                    ),
                    "observation_truncated_terminal_invalid",
                )
            else:
                _require(
                    observed["terminal_event_present"] is True
                    and event_types[-1] == expected_terminal_type,
                    "observation_terminal_event_invalid",
                )
            _require(
                observed["completed_event_present"] is False,
                "observation_terminal_completed_invalid",
            )
    for terminal in ("incomplete", "failed", "truncated"):
        _require(
            scenarios[f"cli_terminal_{terminal}"]["observed"][
                "served_event_envelopes"
            ]
            == scenarios[f"desktop_terminal_{terminal}"]["observed"][
                "served_event_envelopes"
            ],
            "observation_terminal_client_shape_mismatch",
        )

    app_observed = scenarios["desktop_app_v2_lifecycle"]["observed"]
    _require(isinstance(app_observed, Mapping), "observation_desktop_app_invalid")
    _require_exact_fields(
        app_observed,
        {
            "declaration",
            "requests_by_phase",
            "notifications",
            "thread_read_before_restart",
            "thread_read_after_restart",
            "resume_result_fields",
            "resume_result_field_types",
            "identity_relationships",
        },
        "observation_desktop_app_fields_invalid",
    )
    _require(
        app_observed["declaration"] == _expected_observed_declaration(V2),
        "observation_desktop_app_declaration_invalid",
    )
    _validate_v2_phase_requests(app_observed["requests_by_phase"], includes_restart=False)
    notifications = app_observed["notifications"]
    _require(isinstance(notifications, list) and notifications, "observation_notifications_invalid")
    observed_notification_pairs = {
        (entry.get("method"), entry.get("item_type"))
        for entry in notifications
        if isinstance(entry, Mapping)
    }
    required_notification_pairs = {
        ("item/started", "agentMessage"),
        ("item/completed", "agentMessage"),
        ("item/started", "collabAgentToolCall"),
        ("item/completed", "collabAgentToolCall"),
        ("item/started", "subAgentActivity"),
        ("item/completed", "subAgentActivity"),
        ("item/agentMessage/delta", None),
    }
    _require(
        required_notification_pairs.issubset(observed_notification_pairs),
        "observation_notification_lifecycle_invalid",
    )
    notification_enum_values = {
        "item_kind": {"started", "completed"},
        "item_status": {"inProgress", "completed", "failed"},
        "item_tool": {
            "spawnAgent",
            "sendMessage",
            "followupTask",
            "wait",
            "interruptAgent",
            "listAgents",
        },
        "item_phase": {"commentary", "final"},
    }
    for entry in notifications:
        _require(isinstance(entry, Mapping), "observation_notification_invalid")
        base_fields = {"method", "params_fields", "params_field_types"}
        if "item_type" in entry:
            allowed_fields = base_fields | {
                "item_type",
                "item_fields",
                "item_field_types",
                "item_kind",
                "item_status",
                "item_tool",
                "item_phase",
            }
            _require(
                base_fields
                | {"item_type", "item_fields", "item_field_types"}
                <= set(entry)
                <= allowed_fields,
                "observation_notification_fields_invalid",
            )
            _require(
                entry.get("item_fields")
                == sorted(entry.get("item_field_types", {})),
                "observation_notification_item_type_keys_invalid",
            )
            _require(
                entry.get("item_type")
                in {"agentMessage", "collabAgentToolCall", "subAgentActivity"},
                "observation_notification_item_type_invalid",
            )
        else:
            _require(set(entry) == base_fields, "observation_notification_fields_invalid")
            _require(
                entry.get("method") == "item/agentMessage/delta",
                "observation_notification_method_invalid",
            )
        _require(
            entry.get("params_fields")
            == sorted(entry.get("params_field_types", {})),
            "observation_notification_param_type_keys_invalid",
        )
        for field, allowed in notification_enum_values.items():
            if field in entry:
                _require(entry[field] in allowed, "observation_notification_enum_invalid")
    _validate_thread_structure(app_observed["thread_read_before_restart"])
    _validate_thread_structure(app_observed["thread_read_after_restart"])
    _require(
        app_observed["thread_read_before_restart"]
        == app_observed["thread_read_after_restart"],
        "observation_thread_readback_mismatch",
    )
    _require_all_true(
        app_observed["identity_relationships"],
        "observation_desktop_identity_relationship_invalid",
    )

    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    _require(
        re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            serialized,
            re.IGNORECASE,
        )
        is None,
        "observations_opaque_identifier_retained",
    )


def _load_runtime_observations(path: Path) -> dict[str, Any]:
    payload = _load(path, code="observations_file_invalid")
    validate_runtime_observations(payload)
    return payload


def build_contract(observations: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if observations is None:
        observations = _load_runtime_observations(DEFAULT_OBSERVATIONS)
    validate_runtime_observations(observations)
    scenarios = observations["scenarios"]
    cli_scenarios = [
        "cli_v1_request",
        "cli_v2_lifecycle",
        "cli_terminal_incomplete",
        "cli_terminal_failed",
        "cli_terminal_truncated",
    ]
    desktop_scenarios = [
        "desktop_v1_request",
        "desktop_v2_lifecycle",
        "desktop_terminal_incomplete",
        "desktop_terminal_failed",
        "desktop_terminal_truncated",
        "desktop_app_v2_lifecycle",
    ]
    cli_home_bindings = [
        scenarios[name]["home_binding_sha256"] for name in cli_scenarios
    ]
    desktop_home_bindings = [
        scenarios[name]["home_binding_sha256"] for name in desktop_scenarios
    ]
    cli_lifecycle = scenarios["cli_v2_lifecycle"]["observed"]
    desktop_lifecycle = scenarios["desktop_v2_lifecycle"]["observed"]
    desktop_app = scenarios["desktop_app_v2_lifecycle"]["observed"]
    cli_function_sequence = next(
        sequence["events"]
        for sequence in cli_lifecycle["served_event_sequences"]
        if any(
            event["type"] == "response.function_call_arguments.delta"
            for event in sequence["events"]
        )
    )
    function_core_types = {
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
    }
    function_event_order = [
        event["type"]
        for event in cli_function_sequence
        if event["type"] in function_core_types
    ]
    function_event_shapes = {
        event["type"]: copy.deepcopy(event)
        for event in cli_function_sequence
        if event["type"] in function_core_types
    }
    terminal_controls = {
        client: {
            terminal: copy.deepcopy(
                scenarios[f"{client}_terminal_{terminal}"]["observed"]
            )
            for terminal in ("incomplete", "failed", "truncated")
        }
        for client in ("cli", "desktop")
    }
    completed_event = next(
        copy.deepcopy(event)
        for event in cli_function_sequence
        if event["type"] == "response.completed"
    )
    incomplete_event = copy.deepcopy(
        terminal_controls["cli"]["incomplete"]["served_event_envelopes"][-1]
    )
    failed_event = copy.deepcopy(
        terminal_controls["cli"]["failed"]["served_event_envelopes"][-1]
    )
    incomplete_events = terminal_controls["cli"]["incomplete"][
        "served_event_envelopes"
    ]
    failed_events = terminal_controls["cli"]["failed"]["served_event_envelopes"]
    truncated_events = terminal_controls["cli"]["truncated"][
        "served_event_envelopes"
    ]
    terminal_event_boundaries = {
        "response.completed": {
            "previous_event": cli_function_sequence[-2]["type"]
        },
        "response.incomplete": {
            "previous_event": incomplete_events[-2]["type"]
        },
        "response.failed": {"previous_event": failed_events[-2]["type"]},
        "stream_close_without_terminal": {
            "last_observed_event": truncated_events[-1]["type"]
        },
    }
    return {
        "schema": SCHEMA,
        "qualification_status": "accepted_request_call_result_agent_message_and_readback",
        "candidate_revision": "be10f62f44b22fa8c84510238250ae11fb3ecab4",
        "captured_on": observations["captured_on"],
        "source_observations": {
            "schema": observations["schema"],
            "canonical_sha256": _canonical_digest(observations),
            "capture_run_binding_sha256": observations[
                "capture_run_binding_sha256"
            ],
            "path": str(DEFAULT_OBSERVATIONS).replace("\\", "/"),
        },
        "capture_scope": {
            "home": "new_isolated_home_per_runtime",
            "upstream": "loopback_protocol_controlled_responses",
            "existing_user_home_read": False,
            "existing_user_task_read": False,
            "known_crash_task_read": False,
            "content_or_credentials_retained": False,
            "opaque_identity_retained": False,
            "isolated_observations": [
                "cli_v1_request",
                "cli_v2_request_call_result_agent_message_and_restart",
                "desktop_v1_request",
                "desktop_v2_request_call_result_agent_message_and_restart",
                "desktop_app_thread_items_notifications_and_restart",
            ],
            "isolated_home_bindings_sha256": [
                scenarios[name]["home_binding_sha256"] for name in sorted(scenarios)
            ],
        },
        "runtimes": [
            _runtime(
                runtime_observation=observations["runtimes"]["codex_cli"],
                desktop_app=False,
                scenario_names=cli_scenarios,
                home_bindings=cli_home_bindings,
            ),
            _runtime(
                runtime_observation=observations["runtimes"]["codex_desktop"],
                desktop_app=True,
                scenario_names=desktop_scenarios,
                home_bindings=desktop_home_bindings,
            ),
        ],
        "selection_contract": {
            "stage": "before_schema_repair_state_or_scheduler",
            "request_metadata_version_field": "absent_in_both_frozen_runtimes",
            "tool_choice": {
                "observed_value": "auto",
                "included_in_version_discriminator": False,
                "unexpected_value": "fail_closed_for_frozen_runtime_contract",
            },
            "markers": {
                V1: {
                    "wire_type": "namespace",
                    "namespace": V1_NAMESPACE,
                    "tool_names": list(V1_TOOLS),
                    "argument_contract": _argument_contract(V1),
                },
                V2: {
                    "wire_type": "namespace",
                    "namespace": V2_NAMESPACE,
                    "tool_names": list(V2_TOOLS),
                    "argument_contract": _argument_contract(V2),
                },
            },
            "shared_tool_name_policy": "never_classify_from_child_name_alone",
            "direct_function_policy": "not_a_frozen_runtime_collaboration_marker",
            "matching_policy": "complete_namespace_and_child_schema",
            "description_policy": "require_string_but_exclude_dynamic_text_from_matching",
            "child_policy": "exact_type_name_strict_parameter_shape_and_no_output_schema",
            "unknown_missing_duplicate_conflicting_or_mixed": "fail_closed",
            "unexpected_multi_agent_version_field": "fail_closed",
        },
        "protocols": {
            V1: {
                "namespace": V1_NAMESPACE,
                "owner": "codex_client",
                "declaration": {
                    "type": "namespace",
                    "fields": ["type", "name", "description", "tools"],
                    "child_type": "function",
                    "child_fields": [
                        "type",
                        "name",
                        "description",
                        "strict",
                        "parameters",
                    ],
                    "child_strict": False,
                    "child_output_schema_field": "absent",
                    "tool_names": list(V1_TOOLS),
                    "normalized_parameter_schemas": copy.deepcopy(
                        EXPECTED_PARAMETER_SCHEMAS[V1]
                    ),
                },
                "call": {
                    "type": "function_call",
                    "fields": ["type", "id", "name", "namespace", "arguments", "call_id"],
                    "identity_fields": ["id", "call_id"],
                },
                "result": {
                    "type": "function_call_output",
                    "fields": ["type", "id", "call_id", "output"],
                    "identity_fields": ["id", "call_id"],
                    "output_wire_encoding": "json_text_string",
                    "tool_output_schemas": copy.deepcopy(EXPECTED_OUTPUT_SCHEMAS[V1]),
                    "spawn_identity": "agent_id",
                },
                "target_identity": "agent_id",
            },
            V2: {
                "namespace": V2_NAMESPACE,
                "owner": "codex_client",
                "declaration": {
                    "type": "namespace",
                    "fields": ["type", "name", "description", "tools"],
                    "child_type": "function",
                    "child_fields": [
                        "type",
                        "name",
                        "description",
                        "strict",
                        "parameters",
                    ],
                    "child_strict": False,
                    "child_output_schema_field": "absent",
                    "tool_names": list(V2_TOOLS),
                    "normalized_parameter_schemas": copy.deepcopy(
                        EXPECTED_PARAMETER_SCHEMAS[V2]
                    ),
                },
                "call": {
                    "type": "function_call",
                    "fields": ["type", "id", "name", "namespace", "arguments", "call_id"],
                    "identity_fields": ["id", "call_id"],
                },
                "result": {
                    "type": "function_call_output",
                    "fields": ["type", "id", "call_id", "output"],
                    "identity_fields": ["id", "call_id"],
                    "output_wire_encoding": {
                        "followup_task": "plain_text",
                        "interrupt_agent": "json_text_string",
                        "list_agents": "json_text_string",
                        "send_message": "plain_text",
                        "spawn_agent": "json_text_string",
                        "wait_agent": "json_text_string",
                    },
                    "tool_output_schemas": copy.deepcopy(EXPECTED_OUTPUT_SCHEMAS[V2]),
                    "spawn_identity": "task_name",
                    "spawn_output_fields_default": ["task_name"],
                },
                "target_identity": "relative_or_canonical_task_name",
                "canonical_task_identity": "agent_path_serialized_as_task_name",
                "continuation_id_field": "not_present",
            },
        },
        "wire_lifecycle": {
            "function_call_stream": {
                "event_order": function_event_order,
                "event_shapes": function_event_shapes,
                "captured_event_sequences": {
                    "codex_cli": cli_lifecycle["served_event_sequences"],
                    "codex_desktop": desktop_lifecycle["served_event_sequences"],
                },
                "assembly_rule": "ordered_delta_assembly_must_equal_done_arguments",
            },
            "terminal": {
                "event_boundaries": terminal_event_boundaries,
                "events": [
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ],
                "event_shapes": {
                    "response.completed": completed_event,
                    "response.incomplete": incomplete_event,
                    "response.failed": failed_event,
                },
                "controls_by_client": terminal_controls,
                "stream_close_without_completed": "rejected_nonzero_exit",
            },
            "history_items": ["function_call", "function_call_output", "agent_message"],
            "request_replay_envelopes": {
                "codex_cli": cli_lifecycle["requests_by_phase"],
                "codex_desktop": desktop_lifecycle["requests_by_phase"],
            },
            "rollout_readback_envelopes": {
                "codex_cli": cli_lifecycle["rollout_readback"],
                "codex_desktop": desktop_lifecycle["rollout_readback"],
            },
            "same_home_readback": {
                "clients": ["codex_cli", "codex_desktop"],
                "call_namespace_preserved": True,
                "call_and_output_ids_preserved": True,
                "call_output_order_preserved": True,
                "agent_message_author_recipient_and_content_kind_preserved": True,
                "session_meta_multi_agent_version": "v2",
                "turn_context_multi_agent_version": "v2",
                "desktop_thread_resume_and_read": "observed",
                "cli_identity_relationships": cli_lifecycle[
                    "identity_relationships"
                ],
                "desktop_identity_relationships": desktop_lifecycle[
                    "identity_relationships"
                ],
                "desktop_app_identity_relationships": desktop_app[
                    "identity_relationships"
                ],
            },
        },
        "protocol_layers": {
            "client_dispatch": {
                "recipient_namespace": "collaboration",
                "meaning": "client_tool_dispatch_group",
                "wire_discriminator_by_itself": False,
            },
            "responses_declaration_and_call": {
                "namespace": V2_NAMESPACE,
                "declaration_type": "namespace",
                "call_type": "function_call",
                "call_namespace_field": True,
                "tool_choice": "auto",
            },
            "model_input_agent_message": {
                "type": "agent_message",
                "fields": ["type", "id", "author", "recipient", "content"],
                "task_identity_fields": ["author", "recipient"],
                "content_variants": {
                    "input_text": ["type", "text"],
                    "encrypted_content": ["type", "encrypted_content"],
                },
                "source_optional_field": "internal_chat_message_metadata_passthrough",
                "function_result": False,
            },
            "rollout_metadata": {
                "session_meta_field": "multi_agent_version",
                "turn_context_field": "multi_agent_version",
                "values": ["v1", "v2"],
                "sent_as_request_metadata": False,
            },
            "desktop_app": {
                "thread_items": {
                    "agentMessage": ["type", "id", "text", "phase", "memoryCitation"],
                    "collabAgentToolCall": [
                        "type",
                        "id",
                        "tool",
                        "status",
                        "senderThreadId",
                        "receiverThreadIds",
                        "prompt",
                        "model",
                        "reasoningEffort",
                        "agentsStates",
                    ],
                    "subAgentActivity": [
                        "type",
                        "id",
                        "kind",
                        "agentThreadId",
                        "agentPath",
                    ],
                },
                "item_notifications": {
                    "agentMessage": [
                        "item/started",
                        "item/agentMessage/delta",
                        "item/completed",
                    ],
                    "collabAgentToolCall": ["item/started", "item/completed"],
                    "subAgentActivity": ["item/started", "item/completed"],
                },
                "notification_envelopes": desktop_app["notifications"],
                "thread_read_envelope": desktop_app[
                    "thread_read_before_restart"
                ],
                "thread_resume_result": {
                    "fields": desktop_app["resume_result_fields"],
                    "field_types": desktop_app["resume_result_field_types"],
                },
                "turn_terminal_notification": "turn/completed",
                "same_home_thread_resume_and_read": "observed",
                "wire_item_equivalent": False,
            },
        },
        "gateway_boundary": {
            "allowed": "reversible_wire_adaptation_only",
            "native_namespace": "pass_without_semantic_mutation",
            "adaptation_requirements": [
                "injective_request_scoped_aliases",
                "preserve_namespace_and_child_name",
                "preserve_call_and_item_identity",
                "preserve_order_and_stream_boundaries",
                "preserve_agent_message_author_and_recipient",
                "preserve_rollout_and_same_home_replay_semantics",
            ],
            "forbidden": [
                "agent_execution",
                "agent_scheduling",
                "identity_fabrication",
                "v2_to_v1_downgrade",
                "model_or_provider_fallback",
                "history_rewrite",
            ],
        },
        "deferred_to_issue_283": [
            "full_six_tool_two_child_lifecycle",
            "cross_home_negative_cases",
            "malformed_agent_message_and_identity_negative_cases",
        ],
    }


def _namespace_candidates(tools: Sequence[Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        if tool.get("type") == "namespace" and tool.get("name") in {
            V1_NAMESPACE,
            V2_NAMESPACE,
        }:
            candidates.append(tool)
    return candidates


def _conflicting_collaboration_markers(
    tools: Sequence[Any], candidates: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    candidate_ids = {id(candidate) for candidate in candidates}
    collaboration_names = {V1_NAMESPACE, V2_NAMESPACE}
    child_names = set(V1_TOOLS) | set(V2_TOOLS)
    conflicts: list[Mapping[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping) or id(tool) in candidate_ids:
            continue
        name = tool.get("name")
        if name in collaboration_names or (
            tool.get("type") == "function" and name in child_names
        ):
            conflicts.append(tool)
    return conflicts


def _normalize_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if key == "description":
                continue
            if key == "properties" and isinstance(child, Mapping):
                normalized[key] = {
                    property_name: _normalize_schema(property_schema)
                    for property_name, property_schema in child.items()
                }
            else:
                normalized[key] = _normalize_schema(child)
        if normalized.get("type") == "object":
            normalized.setdefault("required", [])
            required = normalized["required"]
            if isinstance(required, list) and all(
                isinstance(field, str) for field in required
            ):
                normalized["required"] = sorted(required)
        return normalized
    if isinstance(value, list):
        return [_normalize_schema(child) for child in value]
    return value


def classify_tools(tools: Sequence[Any]) -> str:
    """Delegate declaration classification to the production #392 contract."""

    try:
        return _classify_production_tools(tools)
    except _ProductionContractError as exc:
        raise ContractValidationError(exc.classification) from exc


def classify_request(request: Mapping[str, Any]) -> str:
    try:
        version = _classify_production_request(request)
    except _ProductionContractError as exc:
        raise ContractValidationError(exc.classification) from exc
    _require(version is not None, "collaboration_marker_missing")
    return version


def validate_contract(
    payload: Mapping[str, Any], observations: Mapping[str, Any] | None = None
) -> None:
    expected = build_contract(observations)
    _require(isinstance(payload, Mapping), "contract_invalid")
    _require(payload.get("schema") == SCHEMA, "contract_schema_invalid")
    _require(dict(payload) == expected, "contract_content_invalid")


def reconcile_contract(
    payload: Mapping[str, Any], observations: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    try:
        validate_contract(payload, observations)
    except ContractValidationError as error:
        return {"reconciled": False, "mismatches": [str(error)]}
    return {"reconciled": True, "mismatches": []}


def replay_contract(payload: Mapping[str, Any], case: str) -> dict[str, Any]:
    _require(case in {"mutation", "deletion", "loss"}, "replay_case_invalid")
    clone = copy.deepcopy(dict(payload))
    if case == "mutation":
        clone["selection_contract"]["markers"][V2]["namespace"] = V1_NAMESPACE
    elif case == "deletion":
        del clone["protocol_layers"]["model_input_agent_message"]
    else:
        clone["runtimes"][1]["source_files"] = {}
    return clone


def _load(path: Path, *, code: str = "contract_file_invalid") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractValidationError(code) from error
    _require(isinstance(value, dict), code)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--source-observations", type=Path, default=DEFAULT_OBSERVATIONS
    )
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        observations = _load_runtime_observations(args.source_observations)
        expected = build_contract(observations)
        validate_contract(expected, observations)
        if args.check:
            report = reconcile_contract(_load(args.out), observations)
            print(json.dumps(report, sort_keys=True))
            return 0 if report["reconciled"] else 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(expected, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(json.dumps({"schema": SCHEMA, "reconciled": True}, sort_keys=True))
        return 0
    except ContractValidationError as error:
        print(f"CONTRACT_INVALID:{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
