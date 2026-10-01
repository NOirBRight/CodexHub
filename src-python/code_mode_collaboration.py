"""Caller-declared Code Mode collaboration, without executing caller JavaScript.

The current exec catalogue carries typed handler declarations in its description.
Only complete, supported signatures confer authority; a mention of a handler does
not. Assignment provenance is explicitly supplied by the caller boundary, never
inferred from ciphertext or request metadata. All state is request-owned.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from collaboration_runtime_contract import (
    COLLABORATION_V2, EXPECTED_PARAMETER_SCHEMAS, CollaborationContractError,
    classify_collaboration_tools, validate_agent_message,
)

_NAMES = frozenset(EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2])
_MESSAGE_NAMES = frozenset({"spawn_agent", "send_message", "followup_task"})
_SECTION = re.compile(r"^### `collaboration__(\w+)`\s*$", re.MULTILINE)
_SIGNATURE = re.compile(r"declare const tools:\s*\{\s*collaboration__(\w+)\(args:\s*\{(.*?)\}\):\s*Promise<", re.DOTALL)
_FIELD = re.compile(r"([a-z_]+)(\?)?:\s*(string|number|boolean)(?:\s*\|\s*(null))?\s*;")


def _without_descriptions(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _without_descriptions(child) for key, child in value.items() if key not in {"description", "encrypted"}}
    if isinstance(value, list):
        return [_without_descriptions(child) for child in value]
    return value


def _exec_tools(tools: Any) -> Iterator[Mapping[str, Any]]:
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        children = (
            tool.get("tools")
            if tool.get("type") == "namespace" and tool.get("name") == "functions"
            else [tool]
        )
        if isinstance(children, list):
            for child in children:
                if (
                    isinstance(child, Mapping)
                    and child.get("type") == "custom"
                    and child.get("name") == "exec"
                ):
                    yield child


def has_code_mode_exec(tools: Any) -> bool:
    """Recognize the caller's actual custom exec transport, including namespaces."""
    for child in _exec_tools(tools):
        description = child.get("description")
        if (
            isinstance(description, str)
            and len(description) <= 512 * 1024
            and "All nested tools are available on the global `tools` object" in description
            and isinstance(child.get("format"), Mapping)
        ):
            return True
    return False


def declared_code_mode_handlers(tool: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read the exact supported typed methods from one actual exec declaration."""
    if not has_code_mode_exec([tool]):
        return []
    description = tool.get("description")
    if not isinstance(description, str) or len(description) > 512 * 1024:
        return []
    if "All nested tools are available on the global `tools` object" not in description:
        return []
    sections = list(_SECTION.finditer(description))
    if not sections:
        return []
    names = [section[1] for section in sections]
    if len(set(names)) != len(names) or not set(names) <= _NAMES:
        return []
    children = []
    for index, section in enumerate(sections):
        body = description[section.end():sections[index + 1].start() if index + 1 < len(sections) else len(description)]
        signatures = list(_SIGNATURE.finditer(body))
        if len(signatures) != 1 or signatures[0][1] != section[1]:
            return []
        fields = re.sub(r"//[^\r\n]*", "", signatures[0][2]).strip()
        properties: dict[str, Any] = {}
        required = []
        position = 0
        while position < len(fields):
            match = _FIELD.match(fields, position)
            if match is None or match[1] in properties:
                return []
            field, optional, kind, nullable = match.groups()
            properties[field] = {"type": [kind, "null"] if nullable else kind}
            if not optional:
                required.append(field)
            position = match.end()
            while position < len(fields) and fields[position].isspace():
                position += 1
        schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
        expected = _without_descriptions(EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2][section[1]])
        if section[1] == "spawn_agent" and "agent_type" not in properties:
            expected["properties"].pop("agent_type", None)
        expected["required"] = sorted(expected["required"])
        compared = {**schema, "required": sorted(required)}
        if compared != expected:
            return []
        children.append({"type": "function", "name": section[1], "description": body.strip(), "strict": False, "parameters": schema})
    return children


def expose_declared_collaboration(payload: dict[str, Any]) -> bool:
    """Expose only the methods this caller can execute, retaining tool placement."""
    groups: list[Any] = [payload.get("tools")]
    items = payload.get("input")
    if isinstance(items, list):
        groups.extend(item.get("tools") for item in items if isinstance(item, dict) and item.get("type") == "additional_tools")
    if any(isinstance(tool, Mapping) and tool.get("name") == "collaboration" for group in groups if isinstance(group, list) for tool in group):
        return False
    candidates = []
    for group in groups:
        for child in _exec_tools(group):
            handlers = declared_code_mode_handlers(child)
            if handlers:
                candidates.append((group, handlers))
    # Multiple exec catalogues do not establish one unambiguous authority.
    if len(candidates) != 1:
        return False
    group, handlers = candidates[0]
    # Replace only the group; callers' objects and exec input remain untouched.
    replacement = [*group, {"type": "namespace", "name": "collaboration", "description": "Caller-owned collaboration declared by Code Mode. Use these direct plaintext tools for assignments, messages and followups; the caller executes them.", "tools": handlers}]
    if group is payload.get("tools"):
        payload["tools"] = replacement
    else:
        payload["input"] = [{**item, "tools": replacement} if isinstance(item, dict) and item.get("type") == "additional_tools" and item.get("tools") is group else item for item in items]
    return True


def is_supported_subset(namespace: Mapping[str, Any]) -> bool:
    """Validate a current caller subset by the same frozen child contracts."""
    children = namespace.get("tools")
    if namespace.get("name") != "collaboration" or not isinstance(children, list) or not children:
        return False
    names = [child.get("name") if isinstance(child, Mapping) else None for child in children]
    if not all(isinstance(name, str) for name in names) or len(names) != len(set(names)) or not set(names) <= _NAMES:
        return False
    probe = copy.deepcopy(namespace)
    # Use validation-only missing children; never publish them as caller tools.
    for name in sorted(_NAMES - set(names)):
        probe["tools"].append({"type": "function", "name": name, "description": "validation", "strict": False, "parameters": copy.deepcopy(EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2][name])})
    for child in probe["tools"]:
        parameters = child.get("parameters")
        if not isinstance(parameters, Mapping):
            return False
        properties = parameters.get("properties")
        if not isinstance(properties, Mapping):
            return False
        if child.get("name") in _MESSAGE_NAMES and isinstance(properties.get("message"), dict):
            # The generated direct exposure is deliberately plaintext.
            if properties["message"].get("encrypted", True) is not True:
                return False
            properties["message"]["encrypted"] = True
    try:
        return classify_collaboration_tools([probe]) == COLLABORATION_V2
    except (CollaborationContractError, AttributeError, TypeError):
        return False


@dataclass(frozen=True, slots=True)
class ObservedAssignment:
    """An actually observed assignment, bound to the caller's agent identity."""
    author: str
    recipient: str
    message: str
    source_call_id: str
    source_item_id: str


def recover_observed_assignments(payload: dict[str, Any], assignments: Sequence[ObservedAssignment]) -> int:
    """Recover exact mislabelled plaintext with unambiguous assignment provenance.

    The owner must supply observations of executed caller assignments, including
    the real Call and typed Item IDs. This function does not invent observations
    by parsing arbitrary generated JavaScript, or store cross-request state.
    Multiple source calls for the same text/address are ambiguous and stay opaque.
    """
    known: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
    for assignment in assignments:
        if all(isinstance(value, str) and value for value in (assignment.author, assignment.recipient, assignment.message, assignment.source_call_id, assignment.source_item_id)):
            key = (assignment.author, assignment.recipient, assignment.message)
            known.setdefault(key, set()).add((assignment.source_call_id, assignment.source_item_id))
    items = payload.get("input")
    if not isinstance(items, list):
        return 0
    count = 0
    result = copy.deepcopy(items)
    for item in result:
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        try:
            validate_agent_message(item)
        except CollaborationContractError:
            continue
        for part in item["content"]:
            if part.get("type") != "encrypted_content":
                continue
            message = part.get("encrypted_content")
            key = (item["author"], item["recipient"], message)
            if len(known.get(key, ())) == 1:
                part.clear()
                part.update(type="input_text", text=message)
                count += 1
    if count:
        payload["input"] = result
    return count
