"""Coerce third-party tool parameter roots to object / object-union schemas.

This is the Gateway compatibility sanitizer for tool ``parameters`` /
``input_schema`` roots. Nested unions and nested type arrays are left alone.
"""

from __future__ import annotations

from typing import Any

from protocol_translation import UnsupportedProtocolTranslationError


def schema_is_object_typed(node: Any) -> bool:
    """Return whether a union branch can be an object variant.

    Exclusive-required branches such as ``{"required": ["paths"]}`` lack
    ``type`` but are object variants once annotated. Scalars, arrays, and
    ``null`` are not.
    """

    if node is True:
        return True
    if not isinstance(node, dict):
        return False
    type_value = node.get("type")
    if type_value == "object":
        return True
    if isinstance(type_value, list):
        return "object" in type_value
    # A schema without an explicit type can still accept objects through
    # $ref, allOf, not, or object constraints such as minProperties. Retain
    # those constraints and intersect with type=object when projecting the
    # branch; absence of a recognized keyword is not evidence of a scalar.
    return type_value is None



def object_schema_from_branch(node: Any) -> dict[str, Any]:
    if node is True:
        return {"type": "object"}
    if not isinstance(node, dict):
        return {"type": "object", "properties": {}}
    next_node = dict(node)
    type_value = next_node.get("type")
    if isinstance(type_value, list) and "object" in type_value:
        next_node["type"] = "object"
    elif type_value != "object":
        next_node["type"] = "object"
    return next_node


def empty_object_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


def coerce_tool_parameter_root(schema: Any) -> tuple[Any, bool]:
    """Force tool parameter roots into an object or all-object union.

    Nested unions, nested type arrays, ``strict``, and ``reasoning`` stay
    untouched. A root ``anyOf`` / ``oneOf`` of objects is kept; non-object
    branches are dropped. Unconstrained roots become objects; impossible and
    scalar roots are rejected rather than broadening or wrapping arguments.
    """

    if schema is False or (isinstance(schema, dict) and "not" in schema
                           and (schema["not"] == {} or schema["not"] is True)):
        raise UnsupportedProtocolTranslationError(
            "unsupported_tool_parameter_root", "Tool parameters cannot satisfy an impossible root schema."
        )
    if isinstance(schema, bool) or not isinstance(schema, dict):
        return empty_object_schema(), True

    next_node = dict(schema)
    changed = False
    root_type = next_node.get("type")
    if isinstance(root_type, list) and "object" in root_type:
        next_node["type"] = "object"
        changed = True
    elif root_type is not None and root_type != "object":
        raise UnsupportedProtocolTranslationError(
            "unsupported_tool_parameter_root", "Tool parameters require an object root."
        )
    union_key = next(
        (
            key
            for key in ("anyOf", "oneOf")
            if isinstance(next_node.get(key), list) and next_node.get(key)
        ),
        None,
    )
    if union_key is not None:
        raw_branches = list(next_node[union_key])
        object_branches = [
            object_schema_from_branch(branch)
            for branch in raw_branches
            if schema_is_object_typed(branch)
        ]
        dropped = len(object_branches) != len(raw_branches)
        if not object_branches:
            raise UnsupportedProtocolTranslationError(
                "unsupported_tool_parameter_root", "Tool parameters require an object branch."
            )
        elif dropped and len(object_branches) == 1:
            # A union branch is conjunctive with its outer schema. Flatten only
            # when there are no outer constraints to overwrite.
            if set(next_node) <= {union_key, "type", "description", "title"}:
                del next_node[union_key]
                next_node.update(object_branches[0])
            else:
                next_node[union_key] = object_branches
            next_node["type"] = "object"
            changed = True
        else:
            if dropped or object_branches != raw_branches:
                next_node[union_key] = object_branches
                changed = True
            if dropped and "type" in next_node and next_node.get("type") != "object":
                next_node.pop("type", None)
                changed = True

    type_value = next_node.get("type")
    has_object_union = any(
        isinstance(next_node.get(key), list) and next_node.get(key)
        for key in ("anyOf", "oneOf")
    )
    if has_object_union:
        return next_node, changed
    if isinstance(type_value, list):
        if "object" in type_value:
            next_node["type"] = "object"
            return next_node, True
        raise UnsupportedProtocolTranslationError(
            "unsupported_tool_parameter_root", "Tool parameters require an object root."
        )
    if type_value is None:
        next_node["type"] = "object"
        return next_node, True
    if type_value != "object":
        raise UnsupportedProtocolTranslationError(
            "unsupported_tool_parameter_root", "Scalar tool parameters cannot be adapted losslessly."
        )
    return next_node, changed
