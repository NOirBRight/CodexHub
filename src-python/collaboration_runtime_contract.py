"""Frozen Codex Collaboration Responses declaration contract.

The schemas below are generated from the accepted issue #392 runtime capture
for Codex CLI 0.146.1 and Codex Desktop 26.803.5235.0.  Production never reads
the evidence artifact: request classification is a pure, local structural
check and dynamic description text is deliberately excluded from matching.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


COLLABORATION_V1 = "collaboration_v1"
COLLABORATION_V2 = "collaboration_v2"
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


class CollaborationContractError(ValueError):
    """Stable bounded failure; request values are never included."""

    def __init__(self, classification: str) -> None:
        self.classification = classification
        super().__init__(classification)


def _require(condition: bool, classification: str) -> None:
    if not condition:
        raise CollaborationContractError(classification)


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
ENCRYPTED_STRING = {"type": "string", "encrypted": True}
NULLABLE_STRING = {"type": ["string", "null"]}
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


# Exact normalized schemas emitted by both frozen clients.  Only description
# annotations are dynamic and ignored by ``_normalize_schema``.
EXPECTED_PARAMETER_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    COLLABORATION_V1: {
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
    COLLABORATION_V2: {
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
    COLLABORATION_V1: {
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
    COLLABORATION_V2: {
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


def _namespace_candidates(tools: Sequence[Any]) -> list[Mapping[str, Any]]:
    return [
        tool
        for tool in tools
        if isinstance(tool, Mapping)
        and tool.get("type") == "namespace"
        and tool.get("name") in {V1_NAMESPACE, V2_NAMESPACE}
    ]


def _has_collaboration_marker(tools: Any) -> bool:
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes, bytearray)):
        return False
    collaboration_names = {V1_NAMESPACE, V2_NAMESPACE}
    child_names = set(V1_TOOLS) | set(V2_TOOLS)
    return any(
        isinstance(tool, Mapping)
        and (
            tool.get("name") in collaboration_names
            or (tool.get("type") == "function" and tool.get("name") in child_names)
        )
        for tool in tools
    )


def classify_collaboration_tools(tools: Sequence[Any]) -> str:
    """Classify the exact frozen declaration surface, or fail closed."""

    _require(
        isinstance(tools, Sequence) and not isinstance(tools, (str, bytes, bytearray)),
        "tools_invalid",
    )
    candidates = _namespace_candidates(tools)
    _require(bool(candidates), "collaboration_marker_missing")
    candidate_ids = {id(candidate) for candidate in candidates}
    collaboration_names = {V1_NAMESPACE, V2_NAMESPACE}
    child_names = set(V1_TOOLS) | set(V2_TOOLS)
    conflicts = [
        tool
        for tool in tools
        if isinstance(tool, Mapping)
        and id(tool) not in candidate_ids
        and (
            tool.get("name") in collaboration_names
            or (tool.get("type") == "function" and tool.get("name") in child_names)
        )
    ]
    _require(
        len(candidates) == 1 and not conflicts,
        "collaboration_marker_duplicate_or_mixed",
    )
    candidate = candidates[0]
    version = (
        COLLABORATION_V1
        if candidate.get("name") == V1_NAMESPACE
        else COLLABORATION_V2
    )
    expected_names = set(V1_TOOLS if version == COLLABORATION_V1 else V2_TOOLS)
    _require(
        set(candidate) == {"type", "name", "description", "tools"},
        "namespace_fields_invalid",
    )
    _require(
        isinstance(candidate.get("description"), str),
        "namespace_description_invalid",
    )
    children = candidate.get("tools")
    _require(isinstance(children, list), "namespace_children_invalid")
    _require(
        all(isinstance(child, Mapping) for child in children),
        "namespace_child_invalid",
    )
    names = [child.get("name") for child in children]
    _require(
        all(child.get("type") == "function" for child in children),
        "namespace_child_type_invalid",
    )
    _require(
        all(isinstance(child.get("name"), str) for child in children),
        "namespace_child_name_invalid",
    )
    _require(len(names) == len(set(names)), "namespace_child_duplicate")
    _require(set(names) == expected_names, "namespace_child_set_invalid")
    for child in children:
        name = child["name"]
        _require(
            set(child) == {"type", "name", "description", "strict", "parameters"},
            "namespace_child_fields_invalid",
        )
        _require(
            isinstance(child.get("description"), str),
            "namespace_child_description_invalid",
        )
        _require(child.get("strict") is False, "namespace_child_strict_invalid")
        parameters = child.get("parameters")
        _require(
            isinstance(parameters, Mapping),
            "namespace_child_parameters_invalid",
        )
        _require(
            _normalize_schema(parameters)
            == _normalize_schema(EXPECTED_PARAMETER_SCHEMAS[version][name]),
            "namespace_child_parameter_schema_mismatch",
        )
    return version


def _has_unexpected_version_signal(request: Mapping[str, Any]) -> bool:
    if "multi_agent_version" in request:
        return True
    for parent_name in ("metadata", "features", "client_metadata"):
        parent = request.get(parent_name)
        if isinstance(parent, Mapping) and "multi_agent_version" in parent:
            return True
    return False


def classify_collaboration_request(request: Mapping[str, Any]) -> str | None:
    """Return the exact request version or ``None`` when no marker exists."""

    _require(isinstance(request, Mapping), "request_invalid")
    _require(
        not _has_unexpected_version_signal(request),
        "collaboration_version_signal_unexpected",
    )
    tools = request.get("tools")
    if not _has_collaboration_marker(tools):
        return None
    _require(request.get("tool_choice") == "auto", "tool_choice_invalid")
    return classify_collaboration_tools(tools)  # type: ignore[arg-type]


def _json_object_or_value(value: Any, malformed: str) -> Any:
    if not isinstance(value, str):
        return value

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, child in items:
            if key in result:
                raise CollaborationContractError(malformed)
            result[key] = child
        return result

    try:
        return json.loads(value, object_pairs_hook=unique_object)
    except CollaborationContractError:
        raise
    except (TypeError, ValueError):
        raise CollaborationContractError(malformed) from None


def _matches_schema(value: Any, schema: Mapping[str, Any]) -> bool:
    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list):
        return sum(
            1
            for alternative in alternatives
            if isinstance(alternative, Mapping) and _matches_schema(value, alternative)
        ) == 1
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        return any(_matches_schema(value, {**schema, "type": item}) for item in expected_type)
    if expected_type == "null":
        if value is not None:
            return False
    elif expected_type == "string":
        if not isinstance(value, str):
            return False
    elif expected_type == "boolean":
        if type(value) is not bool:
            return False
    elif expected_type == "number":
        if type(value) not in {int, float}:
            return False
    elif expected_type == "array":
        if not isinstance(value, list):
            return False
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping) and not all(
            _matches_schema(item, item_schema) for item in value
        ):
            return False
    elif expected_type == "object":
        if not isinstance(value, Mapping):
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return False
        required = schema.get("required", [])
        if not isinstance(required, list) or not set(required).issubset(value):
            return False
        extras = set(value) - set(properties)
        additional = schema.get("additionalProperties", True)
        if additional is False and extras:
            return False
        if isinstance(additional, Mapping) and not all(
            _matches_schema(value[key], additional) for key in extras
        ):
            return False
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, Mapping) and not _matches_schema(child, child_schema):
                return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    return True


def validate_collaboration_arguments(version: str, name: str, value: Any) -> None:
    schemas = EXPECTED_PARAMETER_SCHEMAS.get(version)
    if schemas is None or name not in schemas:
        raise CollaborationContractError("unknown_collaboration_function")
    if not isinstance(value, str):
        raise CollaborationContractError("collaboration_arguments_wire_type_invalid")
    parsed = _json_object_or_value(value, "malformed_collaboration_arguments")
    if not _matches_schema(parsed, schemas[name]):
        raise CollaborationContractError("collaboration_arguments_schema_mismatch")


def validate_collaboration_result(version: str, name: str, value: Any) -> None:
    schemas = EXPECTED_OUTPUT_SCHEMAS.get(version)
    if schemas is None or name not in schemas:
        raise CollaborationContractError("unknown_collaboration_function")
    if not isinstance(value, str):
        raise CollaborationContractError("collaboration_result_wire_type_invalid")
    parsed = _json_object_or_value(value, "malformed_collaboration_result")
    schema = schemas[name]
    if (schema is None and parsed is not None) or (
        isinstance(schema, Mapping) and not _matches_schema(parsed, schema)
    ):
        raise CollaborationContractError("collaboration_result_schema_mismatch")


def validate_agent_message(value: Mapping[str, Any]) -> None:
    if set(value) != {"type", "id", "author", "recipient", "content"}:
        raise CollaborationContractError("agent_message_fields_invalid")
    if value.get("type") != "agent_message":
        raise CollaborationContractError("agent_message_fields_invalid")
    if not all(
        isinstance(value.get(field), str) and bool(value[field])
        for field in ("id", "author", "recipient")
    ):
        raise CollaborationContractError("agent_message_identity_invalid")
    content = value.get("content")
    if not isinstance(content, list):
        raise CollaborationContractError("agent_message_content_invalid")
    for part in content:
        if not isinstance(part, Mapping):
            raise CollaborationContractError("agent_message_content_invalid")
        part_type = part.get("type")
        if part_type == "input_text":
            valid = set(part) == {"type", "text"} and isinstance(part.get("text"), str)
        elif part_type == "encrypted_content":
            valid = set(part) == {"type", "encrypted_content"} and isinstance(
                part.get("encrypted_content"), str
            )
        else:
            valid = False
        if not valid:
            raise CollaborationContractError("agent_message_content_invalid")


__all__ = [
    "COLLABORATION_V1",
    "COLLABORATION_V2",
    "CollaborationContractError",
    "EXPECTED_PARAMETER_SCHEMAS",
    "EXPECTED_OUTPUT_SCHEMAS",
    "V1_NAMESPACE",
    "V1_TOOLS",
    "V2_NAMESPACE",
    "V2_TOOLS",
    "classify_collaboration_request",
    "classify_collaboration_tools",
    "validate_agent_message",
    "validate_collaboration_arguments",
    "validate_collaboration_result",
]
