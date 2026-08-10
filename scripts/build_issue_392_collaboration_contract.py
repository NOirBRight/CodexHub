#!/usr/bin/env python3
"""Build and validate the frozen Beta4 Collaboration runtime contract.

The artifact is deliberately bounded.  It records source- and runtime-derived
wire shapes, but never stores prompts, credentials, paths, task identifiers,
call identifiers, or message content.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "codexhub.issue392.collaboration-runtime-contract.v1"
DEFAULT_OUTPUT = Path("docs/evidence/issue-392/collaboration-runtime-contract.json")

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
    client: str,
    client_version: str,
    runtime_version: str,
    binary_sha256: str,
    source_tag: str,
    source_commit: str,
    source_blobs: Mapping[str, str],
    desktop_app: bool,
) -> dict[str, Any]:
    return {
        "client": client,
        "client_version": client_version,
        "runtime_version": runtime_version,
        "binary_sha256": binary_sha256,
        "source_tag": source_tag,
        "source_commit": source_commit,
        "source_files": {
            key: {"path": SOURCE_FILES[key], "git_blob": source_blobs[key]}
            for key in SOURCE_FILES
        },
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
    }


def build_contract() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "qualification_status": "accepted_request_call_result_agent_message_and_readback",
        "candidate_revision": "be10f62f44b22fa8c84510238250ae11fb3ecab4",
        "captured_on": "2026-08-10",
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
        },
        "runtimes": [
            _runtime(
                client="codex_cli",
                client_version="0.146.1",
                runtime_version="0.146.1",
                binary_sha256="ae9d865f3d346a1a2a60c4e84775622d74e3e7ef53e0dede9c68b81eab306cca",
                source_tag="rust-v0.146.1",
                source_commit="79b4f03d35962b005b007a015113b38930711665",
                source_blobs=CLI_SOURCE_BLOBS,
                desktop_app=False,
            ),
            _runtime(
                client="codex_desktop",
                client_version="26.803.5235.0",
                runtime_version="0.147.0-alpha.6.5",
                binary_sha256="fb5c760e14cf8fe86e12e49e8a3e7f237af06082d6b9fe1e411e463b7229c916",
                source_tag="rust-v0.147.0-alpha.6.5",
                source_commit="618b8e9111da9f57fe380b09d0f6516e3f343536",
                source_blobs=DESKTOP_SOURCE_BLOBS,
                desktop_app=True,
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
                "event_order": [
                    "response.output_item.added",
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                    "response.output_item.done",
                ],
                "event_shapes": {
                    "response.output_item.added": {
                        "fields": ["type", "output_index", "item"],
                        "item_fields": [
                            "type",
                            "id",
                            "status",
                            "name",
                            "namespace",
                            "arguments",
                            "call_id",
                        ],
                        "status": "in_progress",
                    },
                    "response.function_call_arguments.delta": {
                        "fields": ["type", "item_id", "output_index", "delta"]
                    },
                    "response.function_call_arguments.done": {
                        "fields": ["type", "item_id", "output_index", "arguments"]
                    },
                    "response.output_item.done": {
                        "fields": ["type", "output_index", "item"],
                        "item_fields": [
                            "type",
                            "id",
                            "status",
                            "name",
                            "namespace",
                            "arguments",
                            "call_id",
                        ],
                        "status": "completed",
                    },
                },
                "assembly_rule": "ordered_delta_assembly_must_equal_done_arguments",
            },
            "terminal": {
                "event_order_boundary": "after_output_item_done",
                "events": [
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ],
                "event_shapes": {
                    "response.completed": {
                        "fields": ["type", "response"],
                        "response_fields": ["id", "status", "output", "usage"],
                        "status": "completed",
                    },
                    "response.incomplete": {
                        "fields": ["type", "response"],
                        "response_required_fields": ["id", "status", "incomplete_details"],
                        "status": "incomplete",
                    },
                    "response.failed": {
                        "fields": ["type", "response"],
                        "response_required_fields": ["id", "status", "error"],
                        "status": "failed",
                    },
                },
                "stream_close_without_completed": "terminal_error",
            },
            "history_items": ["function_call", "function_call_output", "agent_message"],
            "same_home_readback": {
                "clients": ["codex_cli", "codex_desktop"],
                "call_namespace_preserved": True,
                "call_and_output_ids_preserved": True,
                "call_output_order_preserved": True,
                "agent_message_author_recipient_and_content_kind_preserved": True,
                "session_meta_multi_agent_version": "v2",
                "turn_context_multi_agent_version": "v2",
                "desktop_thread_resume_and_read": "observed",
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


def _normalize_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized = {
            key: _normalize_schema(child)
            for key, child in value.items()
            if key != "description"
        }
        if normalized.get("type") == "object":
            normalized.setdefault("required", [])
        return normalized
    if isinstance(value, list):
        return [_normalize_schema(child) for child in value]
    return value


def classify_tools(tools: Sequence[Any]) -> str:
    """Classify the exact frozen request declaration surface, or fail closed."""

    _require(isinstance(tools, Sequence) and not isinstance(tools, (str, bytes)), "tools_invalid")
    candidates = _namespace_candidates(tools)
    _require(candidates, "collaboration_marker_missing")
    namespaces = [candidate.get("name") for candidate in candidates]
    _require(len(candidates) == 1, "collaboration_marker_duplicate_or_mixed")
    candidate = candidates[0]
    namespace = namespaces[0]
    version = V1 if namespace == V1_NAMESPACE else V2
    expected_names = set(V1_TOOLS if version == V1 else V2_TOOLS)
    _require(
        set(candidate) == {"type", "name", "description", "tools"},
        "namespace_fields_invalid",
    )
    _require(isinstance(candidate.get("description"), str), "namespace_description_invalid")
    children = candidate.get("tools")
    _require(isinstance(children, list), "namespace_children_invalid")
    _require(all(isinstance(child, Mapping) for child in children), "namespace_child_invalid")
    names = [child.get("name") for child in children]
    _require(all(child.get("type") == "function" for child in children), "namespace_child_type_invalid")
    _require(all(isinstance(child.get("name"), str) for child in children), "namespace_child_name_invalid")
    _require(len(names) == len(set(names)), "namespace_child_duplicate")
    _require(set(names) == expected_names, "namespace_child_set_invalid")
    for child in children:
        name = child["name"]
        _require(
            set(child) == {"type", "name", "description", "strict", "parameters"},
            "namespace_child_fields_invalid",
        )
        _require(isinstance(child.get("description"), str), "namespace_child_description_invalid")
        _require(child.get("strict") is False, "namespace_child_strict_invalid")
        parameters = child.get("parameters")
        _require(isinstance(parameters, Mapping), "namespace_child_parameters_invalid")
        normalized = _normalize_schema(parameters)
        _require(
            normalized == EXPECTED_PARAMETER_SCHEMAS[version][name],
            "namespace_child_parameter_schema_mismatch",
        )
    return version


def classify_request(request: Mapping[str, Any]) -> str:
    _require(isinstance(request, Mapping), "request_invalid")
    _require(request.get("tool_choice") == "auto", "tool_choice_invalid")
    tools = request.get("tools")
    version = classify_tools(tools)  # type: ignore[arg-type]
    markers: list[Any] = []
    if "multi_agent_version" in request:
        markers.append(request.get("multi_agent_version"))
    for parent_name in ("metadata", "features", "client_metadata"):
        parent = request.get(parent_name)
        if isinstance(parent, Mapping) and "multi_agent_version" in parent:
            markers.append(parent.get("multi_agent_version"))
    _require(not markers, "collaboration_version_signal_unexpected")
    return version


def validate_contract(payload: Mapping[str, Any]) -> None:
    expected = build_contract()
    _require(isinstance(payload, Mapping), "contract_invalid")
    _require(payload.get("schema") == SCHEMA, "contract_schema_invalid")
    _require(dict(payload) == expected, "contract_content_invalid")


def reconcile_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        validate_contract(payload)
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


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractValidationError("contract_file_invalid") from error
    _require(isinstance(value, dict), "contract_file_invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        expected = build_contract()
        validate_contract(expected)
        if args.check:
            report = reconcile_contract(_load(args.out))
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
