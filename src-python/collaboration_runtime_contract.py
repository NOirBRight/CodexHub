"""Frozen Codex Collaboration Responses declaration contract.

The schemas below are generated from the accepted issue #392 runtime capture
for Codex CLI 0.146.1 and Codex Desktop 26.803.5235.0.  Production never reads
the evidence artifact: request classification is a pure, local structural
check and dynamic description text is deliberately excluded from matching.
"""

from __future__ import annotations

import json
from decimal import Decimal
import math
from protocol_json import strict_json_loads
from typing import Any, Iterable, Mapping, Sequence


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


def _parameter_schema_matches(version: str, name: str, parameters: Mapping[str, Any]) -> bool:
    normalized = _normalize_schema(parameters)
    expected = _normalize_schema(EXPECTED_PARAMETER_SCHEMAS[version][name])
    if normalized == expected:
        return True
    if version not in {COLLABORATION_V1, COLLABORATION_V2} or name != "spawn_agent":
        return False

    # Roles are configuration-dependent in both protocols. CLI 0.153.4 also
    # omits V1 service_tier when tier selection is unavailable. Match only
    # these optional-field omissions; retain exact types, required fields,
    # encryption markers, and all other declaration constraints.
    optional_fields = {"agent_type"}
    if version == COLLABORATION_V1:
        optional_fields.add("service_tier")
    properties = normalized.get("properties")
    if not isinstance(properties, dict):
        return False
    variant = dict(expected)
    variant["properties"] = {
        field: schema
        for field, schema in expected["properties"].items()
        if field not in optional_fields or field in properties
    }
    return normalized == variant


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
    return any(
        isinstance(tool, Mapping)
        and tool.get("type") == "namespace"
        and tool.get("name") in collaboration_names
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
    # Ordinary functions occupy a different identity from namespace children.
    # Their names cannot select, duplicate, or override Collaboration.
    _require(
        len(candidates) == 1,
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
            _parameter_schema_matches(version, name, parameters),
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
    tools = request.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, Mapping) and tool.get("namespace") in {V1_NAMESPACE, V2_NAMESPACE}:
                # A function with a reserved namespace field is not the
                # client's complete namespace declaration. Do not infer
                # Collaboration authority from that malformed tool shape.
                raise CollaborationContractError("malformed_collaboration_declaration")
    if not _has_collaboration_marker(tools):
        # Ordinary provider requests may carry similarly named metadata and
        # may omit tool_choice.  They are not Collaboration requests unless
        # the exact frozen namespace declaration is present.
        return None
    _require(
        not _has_unexpected_version_signal(request),
        "collaboration_version_signal_unexpected",
    )
    _require(request.get("tool_choice") == "auto", "tool_choice_invalid")
    return classify_collaboration_tools(tools)  # type: ignore[arg-type]


# CLI 0.153.4 exposes these bounds in the native V1/V2 wait handlers.  Keep
# versioned names even though the currently shipped values coincide; a future
# client change must update one contract without silently changing the other.
COLLABORATION_V1_TIMEOUT_MIN = 10_000
COLLABORATION_V1_TIMEOUT_MAX = 3_600_000
COLLABORATION_V2_TIMEOUT_MIN = 10_000
COLLABORATION_V2_TIMEOUT_MAX = 3_600_000


def _json_object_or_value(value: Any, malformed: str) -> Any:
    if not isinstance(value, str):
        return value

    try:
        return strict_json_loads(value)
    except (TypeError, ValueError):
        raise CollaborationContractError(malformed) from None


def _parse_collaboration_arguments(value: str, malformed: str) -> Any:
    """Parse arguments without losing exact timeout-number representations."""
    try:
        return strict_json_loads(value, exact_numbers=True)
    except (TypeError, ValueError):
        raise CollaborationContractError(malformed) from None


def _normalize_timeout_value(value: Any, *, version: str) -> tuple[int, bool]:
    """Return an exact integer timeout, never rounding or truncating."""

    bounds = {
        COLLABORATION_V1: (COLLABORATION_V1_TIMEOUT_MIN, COLLABORATION_V1_TIMEOUT_MAX),
        COLLABORATION_V2: (COLLABORATION_V2_TIMEOUT_MIN, COLLABORATION_V2_TIMEOUT_MAX),
    }.get(version)
    if bounds is None:
        raise CollaborationContractError("unknown_collaboration_version")
    minimum, maximum = bounds
    if type(value) is int:
        normalized = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise CollaborationContractError("collaboration_timeout_not_integer")
        # Compare against the bounded contract before converting to ``int``.
        # A syntactically valid exponent such as ``1e1000000000`` must fail
        # closed without attempting to allocate a billion-digit Python int.
        if value < minimum or value > maximum:
            raise CollaborationContractError("collaboration_timeout_out_of_range")
        normalized = int(value)
    else:
        raise CollaborationContractError("collaboration_timeout_not_integer")
    if not minimum <= normalized <= maximum:
        raise CollaborationContractError("collaboration_timeout_out_of_range")
    return normalized, type(value) is not int or normalized != value


def normalize_collaboration_arguments(version: str, name: str, value: Any) -> tuple[str, bool]:
    """Validate and losslessly canonicalize known Collaboration arguments.

    Only the request-local ``wait_agent.timeout_ms`` field is normalized.  The
    returned wire string is deterministic; callers must use it for the body,
    history, and completed SSE item, while preserving failed history verbatim.
    """

    schemas = EXPECTED_PARAMETER_SCHEMAS.get(version)
    if schemas is None or name not in schemas:
        raise CollaborationContractError("unknown_collaboration_function")
    if not isinstance(value, str):
        raise CollaborationContractError("collaboration_arguments_wire_type_invalid")
    parsed = _parse_collaboration_arguments(value, "malformed_collaboration_arguments")
    changed = False
    if name == "wait_agent" and isinstance(parsed, dict) and "timeout_ms" in parsed:
        timeout, timeout_changed = _normalize_timeout_value(parsed["timeout_ms"], version=version)
        parsed["timeout_ms"] = timeout
        changed = timeout_changed
    if not _matches_schema(parsed, schemas[name]):
        raise CollaborationContractError("collaboration_arguments_schema_mismatch")
    # Preserve the caller's exact JSON spelling for every argument except the
    # one known CLI mismatch (wait_agent.timeout_ms).  Message payloads and
    # failed history must not be rewritten merely because they crossed this
    # boundary.
    if not changed:
        return value, False
    try:
        encoded = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise CollaborationContractError("malformed_collaboration_arguments") from None
    return encoded, changed


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
        if isinstance(value, Decimal):
            if not value.is_finite():
                return False
        elif type(value) not in {int, float} or not math.isfinite(value):
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
    normalize_collaboration_arguments(version, name, value)


def is_client_argument_parse_error(value: Any) -> bool:
    """A bare prefix is not evidence that the client rejected arguments."""
    prefix = "failed to parse function arguments:"
    return isinstance(value, str) and value.startswith(prefix) and bool(value[len(prefix):].strip())


def failed_argument_call_ids(items: Iterable[Any]) -> set[str]:
    """Return only call IDs backed by an earlier, real failed call.

    A result-looking item is not evidence by itself.  In particular, an
    output placed before its call (or an output for an unknown call) must not
    grant the later call the ``preserve_failed_arguments`` exception; doing so
    would let malformed new arguments bypass the normalizer.
    """
    if items is None:
        return set()
    seen_calls: set[str] = set()
    failed: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type in {"function_call", "custom_tool_call"}:
            if isinstance(call_id, str) and call_id:
                seen_calls.add(call_id)
            continue
        if (
            item_type in {"function_call_output", "custom_tool_call_output"}
            and isinstance(call_id, str)
            and call_id in seen_calls
            and is_client_argument_parse_error(item.get("output"))
        ):
            failed.add(call_id)
    return failed


def validate_collaboration_result(version: str, name: str, value: Any) -> None:
    schemas = EXPECTED_OUTPUT_SCHEMAS.get(version)
    if schemas is None or name not in schemas:
        raise CollaborationContractError("unknown_collaboration_function")
    if not isinstance(value, str):
        raise CollaborationContractError("collaboration_result_wire_type_invalid")
    schema = schemas[name]
    # Void-result tools (send_message, followup_task in V2) emit an empty string
    # from the real Codex CLI because their handlers return Rust's unit type.
    # Treat that as the JSON null the contract expects: reversible normalization
    # that keeps the wire value intact while accepting what the client can
    # actually produce.
    if schema is None and value == "":
        parsed: Any = None
    else:
        try:
            parsed = _json_object_or_value(value, "malformed_collaboration_result")
        except CollaborationContractError:
            # Argument deserialization fails before any V2 handler runs.
            # The client records this shared error as plain text (for example
            # a number-schema timeout emitted as 180000.0 but parsed as i64).
            # Replay it unchanged so the model can correct the failed call.
            if version == COLLABORATION_V2 and is_client_argument_parse_error(value):
                return
            # Codex CLI serializes a failed V2 interrupt as the tool's plain
            # error text rather than a JSON result object.  Preserve that
            # replay value; JSON-shaped failures (including duplicate keys)
            # still go through the strict result schema below.
            if version == COLLABORATION_V2 and name == "interrupt_agent" and value.strip():
                try:
                    json.loads(value)
                except (TypeError, ValueError):
                    return
            raise
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
    "failed_argument_call_ids",
    "is_client_argument_parse_error",
    "COLLABORATION_V1",
    "COLLABORATION_V2",
    "COLLABORATION_V1_TIMEOUT_MIN",
    "COLLABORATION_V1_TIMEOUT_MAX",
    "COLLABORATION_V2_TIMEOUT_MIN",
    "COLLABORATION_V2_TIMEOUT_MAX",
    "CollaborationContractError",
    "EXPECTED_PARAMETER_SCHEMAS",
    "EXPECTED_OUTPUT_SCHEMAS",
    "normalize_collaboration_arguments",
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
