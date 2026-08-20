"""Gateway tool-surface adapter.

This module owns Codex tool schema injection/declarations, bounded
`tool_search` call handling, structured tool-input rewriting, and
third-party tool-call surface adaptation. It is deliberately independent of
`codex_proxy`; the facade supplies apply_patch history reconstruction and
internal-message callbacks through a typed `ToolSurfaceAdapter`.

Runtime plan construction stays in `runtime_tool_compatibility`. Collaboration
V1/V2 worker lifecycle stays in `collaboration_adapter`. apply_patch
request/history/response/stream adaptation stays in `apply_patch_adapter`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from codex_semantic_adapter import (
    COLLABORATION_V1,
    COLLABORATION_V1_ALIAS_PREFIXES,
    COLLABORATION_V2,
    CollaborationBoundaryError,
    MULTI_AGENT_TOOL_NAMES as SEMANTIC_MULTI_AGENT_TOOL_NAMES,
    classify_collaboration_payload,
    dump_arguments_like,
    json_argument_string_needs_repair,
    json_object_from_arguments,
    multi_agent_discovery_arguments,
    normalize_multi_agent_arguments,
    normalize_tool_search_arguments,
)
from apply_patch_adapter import APPLY_PATCH_FUNCTION_NAME
from collaboration_adapter import WORKER_REQUESTED_BINDING_FIELD


TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MULTI_AGENT_NAMESPACE_ALIASES = {
    "multi_agent_v1",
    "mcp__multi_agent_v1",
}
NODE_REPL_NAMESPACE = "mcp__node_repl"
LOCAL_TOOL_GATEWAY_NAMESPACE = "mcp__codex_apps__local_tool_gateway_"
APPLY_PATCH_FUNCTION_NAME = "apply_patch"
THIRD_PARTY_TOOL_NAME_ALIASES = {
    f"{prefix}{tool_name}": tool_name
    for prefix in COLLABORATION_V1_ALIAS_PREFIXES
    for tool_name in SEMANTIC_MULTI_AGENT_TOOL_NAMES
}
MULTI_AGENT_DISCOVERY_TOOLS = [
    {
        "type": "namespace",
        "name": "multi_agent_v1",
        "description": "Tools for spawning and managing Codex sub-agents.",
        "tools": [
            {
                "type": "function",
                "name": "spawn_agent",
                "description": "Spawn a sub-agent. Use namespace multi_agent_v1 and function name spawn_agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_type": {"type": "string", "enum": ["worker", "default"]},
                        "fork_context": {"type": "boolean"},
                        "message": {"type": "string"},
                    },
                    "required": ["agent_type"],
                    "additionalProperties": True,
                },
            },
            {
                "type": "function",
                "name": "wait_agent",
                "description": "Wait for one or more spawned sub-agents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "targets": {"type": "array", "items": {"type": "string"}},
                        "timeout_ms": {"type": "number"},
                    },
                    "required": ["targets"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "close_agent",
                "description": "Close a spawned sub-agent when it is no longer needed.",
                "parameters": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "resume_agent",
                "description": "Resume a previously closed sub-agent by id.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "send_input",
                "description": "Send a message to an existing sub-agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "message": {"type": "string"},
                        "interrupt": {"type": "boolean"},
                    },
                    "required": ["target"],
                    "additionalProperties": True,
                },
            },
        ],
    }
]
TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL = {
    "type": "function",
    "name": "tool_search",
    "description": "Discover deferred Codex tools by keyword. Use this before calling a tool that is not already visible.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
TOOL_SEARCH_EMPTY_MISS_BOUND = 2
TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION = "identical_exact_query"
TOOL_SEARCH_UNAVAILABLE_STATUS = "unavailable"
INTERNAL_INPUT_ITEM_TYPES = {
    "compaction",
    "compaction_trigger",
    "reasoning",
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "web_search_call",
    "tool_search_call",
    "tool_search_output",
}


class ApplyPatchHistoryAdapter(Protocol):
    def __call__(
        self,
        input_items: list[Any],
        *,
        event_context: Mapping[str, Any] | None,
    ) -> tuple[list[Any], set[str], bool]: ...


class InternalMessageAdapter(Protocol):
    def __call__(self, item: Mapping[str, Any]) -> dict[str, str] | None: ...


class TranscriptMessageAdapter(Protocol):
    def __call__(self, title: str, item: Mapping[str, Any]) -> dict[str, Any]: ...


class ToolCompatibilityEntryView(Protocol):
    family: str
    disposition: str
    original_name: str | None


class ToolCompatibilityPlanView(Protocol):
    entries: Sequence[ToolCompatibilityEntryView]

    def owns_wire_value(self, value: Any) -> bool: ...


def _passthrough_apply_patch_history(
    input_items: list[Any],
    *,
    event_context: Mapping[str, Any] | None = None,
) -> tuple[list[Any], set[str], bool]:
    _ = event_context
    return input_items, set(), False


def _no_internal_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    _ = item
    return None


def _default_transcript_message(title: str, item: Mapping[str, Any]) -> dict[str, Any]:
    _ = item
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": title}],
    }


@dataclass(frozen=True)
class ToolSurfaceFacts:
    """Immutable tool-surface constants for one adapter."""

    tool_name_re: re.Pattern[str] = TOOL_NAME_RE
    multi_agent_tool_names: frozenset[str] = field(
        default_factory=lambda: frozenset(SEMANTIC_MULTI_AGENT_TOOL_NAMES)
    )
    multi_agent_namespace_aliases: frozenset[str] = field(
        default_factory=lambda: frozenset(MULTI_AGENT_NAMESPACE_ALIASES)
    )
    node_repl_namespace: str = NODE_REPL_NAMESPACE
    local_tool_gateway_namespace: str = LOCAL_TOOL_GATEWAY_NAMESPACE
    apply_patch_function_name: str = APPLY_PATCH_FUNCTION_NAME
    third_party_tool_name_aliases: Mapping[str, str] = field(
        default_factory=lambda: dict(THIRD_PARTY_TOOL_NAME_ALIASES)
    )
    multi_agent_discovery_tools: tuple[Any, ...] = field(
        default_factory=lambda: tuple(MULTI_AGENT_DISCOVERY_TOOLS)
    )
    tool_search_explicit_function_tool: Mapping[str, Any] = field(
        default_factory=lambda: TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL
    )
    tool_search_empty_miss_bound: int = TOOL_SEARCH_EMPTY_MISS_BOUND
    tool_search_unavailable_query_classification: str = (
        TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION
    )
    tool_search_unavailable_status: str = TOOL_SEARCH_UNAVAILABLE_STATUS
    internal_input_item_types: frozenset[str] = field(
        default_factory=lambda: frozenset(INTERNAL_INPUT_ITEM_TYPES)
    )
    worker_requested_binding_field: str = WORKER_REQUESTED_BINDING_FIELD
    collaboration_v1: str = COLLABORATION_V1
    collaboration_v2: str = COLLABORATION_V2


@dataclass(frozen=True)
class ToolSurfaceAdapter:
    """Typed tool-surface seam for declarations, search, history, and aliases."""

    facts: ToolSurfaceFacts = field(default_factory=ToolSurfaceFacts)
    adapt_apply_patch_history: ApplyPatchHistoryAdapter = _passthrough_apply_patch_history
    compatible_internal_message: InternalMessageAdapter = _no_internal_message
    transcript_message: TranscriptMessageAdapter = _default_transcript_message

    def valid_tool_name(self, value: Any) -> bool:
        return isinstance(value, str) and bool(self.facts.tool_name_re.fullmatch(value))

    def is_tool_call_item(self, item: Mapping[str, Any]) -> bool:
        item_type = item.get("type")
        return isinstance(item_type, str) and item_type in {"function_call", "custom_tool_call"}

    def has_invalid_tool_name(self, item: Mapping[str, Any]) -> bool:
        return self.is_tool_call_item(item) and not self.valid_tool_name(item.get("name"))

    def tool_schema_name(self, value: Any) -> str | None:
        if not isinstance(value, Mapping):
            return None
        name = value.get("name")
        return name if isinstance(name, str) and name else None

    def tool_parameters_schema(self, value: Mapping[str, Any]) -> dict[str, Any]:
        for key in ("parameters", "inputSchema", "input_schema"):
            schema = value.get(key)
            if isinstance(schema, dict):
                return dict(schema)
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def explicit_function_tool(
        self,
        name: str,
        description: str,
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": dict(parameters),
        }

    def supports_explicit_namespace_alias(self, namespace_name: str) -> bool:
        return namespace_name == "codex_app" or namespace_name.startswith("mcp__")

    def is_multi_agent_namespace_name(self, name: str | None) -> bool:
        return isinstance(name, str) and name in self.facts.multi_agent_namespace_aliases

    def is_multi_agent_explicit_tool_name(self, name: str) -> bool:
        return name in self.facts.third_party_tool_name_aliases

    def multi_agent_alias_tool_name(self, name: Any) -> str | None:
        if not isinstance(name, str):
            return None
        if name in self.facts.multi_agent_tool_names:
            return name
        return self.facts.third_party_tool_name_aliases.get(name)

    def looks_like_response_tool_name_fragment(self, value: Mapping[str, Any]) -> bool:
        item_type = value.get("type")
        if isinstance(item_type, str) and item_type.startswith("response."):
            return True
        if any(key in value for key in ("call_id", "item_id", "arguments", "status")):
            return True
        return set(value.keys()).issubset({"name", "namespace", "index", "id"})

    def is_multi_agent_tool_schema(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        item_type = value.get("type")
        name = self.tool_schema_name(value)
        if item_type == "namespace":
            return self.is_multi_agent_namespace_name(name)
        if item_type == "function":
            if value.get("namespace") == "multi_agent_v1":
                return True
            return isinstance(name, str) and self.is_multi_agent_explicit_tool_name(name)
        return False

    def is_node_repl_explicit_tool_name(self, name: str) -> bool:
        namespace = self.facts.node_repl_namespace
        return name.startswith(f"{namespace}__") or name.startswith(f"{namespace}.")

    def is_node_repl_tool_schema(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        item_type = value.get("type")
        name = self.tool_schema_name(value)
        namespace = self.facts.node_repl_namespace
        if item_type == "namespace":
            return name == namespace
        if item_type == "function":
            if value.get("namespace") == namespace:
                return True
            return isinstance(name, str) and self.is_node_repl_explicit_tool_name(name)
        return False

    def is_local_tool_gateway_tool_schema(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        name = self.tool_schema_name(value)
        if not isinstance(name, str):
            return False
        local_gateway_namespace = self.facts.local_tool_gateway_namespace
        if value.get("type") == "namespace":
            return name == local_gateway_namespace
        if value.get("type") == "function":
            namespace = value.get("namespace")
            if namespace == local_gateway_namespace:
                return True
            return name.startswith(f"{local_gateway_namespace}__")
        return False

    def is_mcp_or_codex_app_tool_schema(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        name = self.tool_schema_name(value)
        namespace = value.get("namespace")
        if isinstance(namespace, str) and (namespace.startswith("mcp__") or namespace == "codex_app"):
            return True
        if not isinstance(name, str):
            return False
        return name.startswith("mcp__") or name == "codex_app" or name.startswith("codex_app__")

    def is_flattened_namespace_schema(self, value: Any) -> bool:
        if not isinstance(value, Mapping) or value.get("type") != "namespace":
            return False
        name = self.tool_schema_name(value)
        return self.is_multi_agent_namespace_name(name) or (
            isinstance(name, str) and self.supports_explicit_namespace_alias(name)
        )

    def is_raw_namespace_schema(self, value: Any) -> bool:
        return isinstance(value, Mapping) and value.get("type") == "namespace"

    def valid_namespace_function_names(self, value: Any) -> tuple[str, tuple[str, ...]] | None:
        if not self.is_raw_namespace_schema(value):
            return None
        namespace_name = self.tool_schema_name(value)
        namespace_tools = value.get("tools")
        if not isinstance(namespace_name, str) or not namespace_name or not isinstance(namespace_tools, list):
            return None
        child_names: list[str] = []
        for tool in namespace_tools:
            tool_name = self.tool_schema_name(tool)
            if (
                not isinstance(tool, Mapping)
                or tool.get("type") != "function"
                or not isinstance(tool_name, str)
                or not tool_name
            ):
                return None
            child_names.append(tool_name)
        if not child_names or len(set(child_names)) != len(child_names):
            return None
        return namespace_name, tuple(child_names)

    def deferred_namespace_surface_counts(
        self,
        source_tools: list[Any],
        final_tools: list[Any],
    ) -> tuple[int, int]:
        final_function_names = {
            name
            for tool in final_tools
            if isinstance(tool, Mapping) and tool.get("type") == "function"
            for name in (self.tool_schema_name(tool),)
            if name is not None
        }
        final_qualified_functions = {
            (tool.get("namespace"), name)
            for tool in final_tools
            if isinstance(tool, Mapping)
            and tool.get("type") == "function"
            and isinstance(tool.get("namespace"), str)
            for name in (self.tool_schema_name(tool),)
            if name is not None
        }
        final_namespace_children: dict[str, set[str]] = {}
        for tool in final_tools:
            details = self.valid_namespace_function_names(tool)
            if details is None:
                continue
            namespace_name, child_names = details
            final_namespace_children.setdefault(namespace_name, set()).update(child_names)

        namespace_count = 0
        child_count = 0
        for namespace in source_tools:
            details = self.valid_namespace_function_names(namespace)
            if details is None:
                continue
            namespace_name, child_names = details
            surviving_namespace_children = final_namespace_children.get(namespace_name, set())
            if not set(child_names).issubset(surviving_namespace_children):
                namespace_count += 1
            for child_name in child_names:
                if (
                    child_name in surviving_namespace_children
                    or (namespace_name, child_name) in final_qualified_functions
                    or f"{namespace_name}__{child_name}" in final_function_names
                    or f"{namespace_name}.{child_name}" in final_function_names
                ):
                    continue
                child_count += 1
        return namespace_count, child_count

    def flatten_namespace_function_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for namespace in tools:
            if not isinstance(namespace, Mapping) or namespace.get("type") != "namespace":
                continue
            namespace_name = self.tool_schema_name(namespace)
            namespace_tools = namespace.get("tools")
            if (
                not namespace_name
                or not self.valid_tool_name(namespace_name)
                or not self.supports_explicit_namespace_alias(namespace_name)
                or not isinstance(namespace_tools, list)
            ):
                continue
            for tool in namespace_tools:
                if not isinstance(tool, Mapping) or tool.get("type") != "function":
                    continue
                tool_name = self.tool_schema_name(tool)
                if not tool_name or not self.valid_tool_name(tool_name):
                    continue
                alias = f"{namespace_name}__{tool_name}"
                description = str(tool.get("description") or f"Invoke Codex namespace {namespace_name}.{tool_name}.")
                result.append(self.explicit_function_tool(alias, description, self.tool_parameters_schema(tool)))
        return result

    def multi_agent_function_call_name(self, item: Mapping[str, Any]) -> str | None:
        if item.get("type") != "function_call":
            return None
        namespace = item.get("namespace")
        name = item.get("name")
        tool_name = self.multi_agent_alias_tool_name(name)
        if namespace == "multi_agent_v1" and tool_name is not None:
            return tool_name
        if tool_name is not None and name != tool_name:
            return tool_name
        return None

    def node_repl_function_call_name(self, item: Mapping[str, Any]) -> str | None:
        if item.get("type") != "function_call":
            return None
        namespace = item.get("namespace")
        name = item.get("name")
        node_repl = self.facts.node_repl_namespace
        if namespace == node_repl and name == "js":
            return "js"
        if name in {f"{node_repl}__js", f"{node_repl}.js"}:
            return "js"
        return None

    def multi_agent_explicit_function_tools(
        self,
        include_spawn_agent: bool = True,
        include_wait_agent: bool = True,
        include_close_agent: bool = True,
        include_resume_agent: bool = True,
        include_send_input: bool = True,
        open_agent_ids: list[str] | None = None,
        wait_agent_ids: list[str] | None = None,
        close_agent_ids: list[str] | None = None,
        worker_selector_values: tuple[str, ...] = ("worker", "default"),
    ) -> list[dict[str, Any]]:
        namespace = self.facts.multi_agent_discovery_tools[0] if self.facts.multi_agent_discovery_tools else None
        tools = namespace.get("tools") if isinstance(namespace, Mapping) else None
        if not isinstance(tools, list):
            return []

        result: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            name = self.tool_schema_name(tool)
            if not name or name not in self.facts.multi_agent_tool_names:
                continue
            if name == "spawn_agent" and not include_spawn_agent:
                continue
            if name == "wait_agent" and not include_wait_agent:
                continue
            if name == "close_agent" and not include_close_agent:
                continue
            if name == "resume_agent" and not include_resume_agent:
                continue
            if name == "send_input" and not include_send_input:
                continue
            alias = f"multi_agent_v1__{name}"
            description = str(tool.get("description") or f"Invoke Codex multi_agent_v1.{name}.")
            parameters = json.loads(json.dumps(self.tool_parameters_schema(tool)))
            properties = parameters.setdefault("properties", {})
            if name == "spawn_agent" and isinstance(properties, dict):
                agent_type = properties.get("agent_type")
                if isinstance(agent_type, dict):
                    agent_type["enum"] = list(worker_selector_values)
                message = properties.get("message")
                if isinstance(message, dict):
                    message.setdefault(
                        "description",
                        "Complete child-agent task prompt. Include all instructions the child needs.",
                    )
                fork_context = properties.get("fork_context")
                if isinstance(fork_context, dict):
                    fork_context["description"] = (
                        "Set false for self-contained child prompts so the child follows only the supplied message. "
                        "Set true only when inheriting the coordinator transcript is explicitly needed."
                    )
                    fork_context.setdefault("default", False)
            target_agent_ids = open_agent_ids
            if name == "wait_agent" and wait_agent_ids is not None:
                target_agent_ids = wait_agent_ids
            elif name == "close_agent" and close_agent_ids is not None:
                target_agent_ids = close_agent_ids
            if target_agent_ids and name in {"wait_agent", "close_agent"}:
                ids_text = ", ".join(target_agent_ids)
                description += f" Current open agent_id target(s): {ids_text}. Use these id(s) next."
                if isinstance(properties, dict):
                    if name == "wait_agent":
                        targets = properties.get("targets")
                        if isinstance(targets, dict):
                            targets["description"] = (
                                f"MUST be exactly this list for the currently open Codex child agent(s): {list(target_agent_ids)!r}."
                            )
                            targets.setdefault("default", list(target_agent_ids))
                            items = targets.setdefault("items", {})
                            if isinstance(items, dict):
                                items["enum"] = list(target_agent_ids)
                        timeout_ms = properties.get("timeout_ms")
                        if isinstance(timeout_ms, dict):
                            timeout_ms.setdefault("description", "Use 60000 for the standard Codex subagent test.")
                            timeout_ms.setdefault("default", 60000)
                    elif name == "close_agent":
                        target = properties.get("target")
                        if isinstance(target, dict):
                            target["description"] = (
                                f"MUST be one of the already-waited open Codex child agent id(s): {', '.join(target_agent_ids)}."
                            )
                            if len(target_agent_ids) == 1:
                                target.setdefault("default", target_agent_ids[0])
                            target["enum"] = list(target_agent_ids)
            result.append(self.explicit_function_tool(alias, description, parameters))
        return result

    def function_tool_names(self, value: Any) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {
            name
            for tool in value
            if isinstance(tool, Mapping)
            and tool.get("type") == "function"
            and isinstance((name := tool.get("name")), str)
        }

    def codex_apps_flat_alias_parts(self, name: Any) -> tuple[str, str] | None:
        if not isinstance(name, str) or not name.startswith("mcp__codex_apps__"):
            return None
        local_gateway_namespace = self.facts.local_tool_gateway_namespace
        if name.startswith(local_gateway_namespace):
            tool_name = name[len(local_gateway_namespace) :].lstrip("_")
            if self.valid_tool_name(tool_name):
                return local_gateway_namespace, tool_name
        namespace_stem, found, tool_name = name.rpartition("___")
        if not found:
            return None
        namespace = f"{namespace_stem}_"
        if (
            namespace.startswith("mcp__codex_apps__")
            and namespace.endswith("_")
            and self.valid_tool_name(namespace)
            and self.valid_tool_name(tool_name)
        ):
            return namespace, tool_name
        return None

    def codex_apps_flat_alias_name(self, name: Any) -> str | None:
        return name if self.codex_apps_flat_alias_parts(name) is not None else None

    def split_namespace_tool_alias(self, name: Any) -> tuple[str, str] | None:
        if not isinstance(name, str):
            return None
        codex_apps_alias = self.codex_apps_flat_alias_parts(name)
        if codex_apps_alias is not None:
            return codex_apps_alias
        for separator in ("__", "."):
            namespace, found, tool_name = name.rpartition(separator)
            if not found:
                continue
            if (
                self.valid_tool_name(namespace)
                and self.supports_explicit_namespace_alias(namespace)
                and self.valid_tool_name(tool_name)
            ):
                return namespace, tool_name
        return None

    def codex_apps_namespace_flat_alias(self, namespace: Any, name: Any) -> str | None:
        if not (
            isinstance(namespace, str)
            and isinstance(name, str)
            and namespace.startswith("mcp__codex_apps__")
            and namespace.endswith("_")
            and self.valid_tool_name(namespace)
            and self.valid_tool_name(name)
        ):
            return None
        alias = f"{namespace}__{name}"
        return alias if self.valid_tool_name(alias) else None

    def inject_explicit_codex_tools(
        self,
        payload: dict[str, Any],
        include_tool_search: bool = True,
        include_multi_agent_tools: bool = True,
        include_spawn_agent: bool = True,
        include_wait_agent: bool = True,
        include_close_agent: bool = True,
        include_resume_agent: bool = True,
        include_send_input: bool = True,
        include_node_repl_tools: bool = True,
        include_local_tool_gateway_tools: bool = True,
        strip_namespace_tools: bool = True,
        strip_all_namespace_tools: bool = False,
        include_flattened_namespace_tools: bool = True,
        deferred_core_surface: bool = False,
        tool_surface_counts: dict[str, int] | None = None,
        tool_surface_source_tools: list[Any] | None = None,
        open_agent_ids: list[str] | None = None,
        wait_agent_ids: list[str] | None = None,
        close_agent_ids: list[str] | None = None,
        worker_selector_values: tuple[str, ...] = ("worker", "default"),
    ) -> bool:
        if tool_surface_counts is not None:
            tool_surface_counts.update(
                {
                    "namespace_declaration_count": 0,
                    "eager_tool_count": 0,
                    "retained_core_count": 0,
                    "deferred_tool_count": 0,
                }
            )
        tools = payload.get("tools")
        if tools is None:
            tools = []
            payload["tools"] = tools
        if not isinstance(tools, list):
            return False

        changed = False
        surface_source_tools = list(tool_surface_source_tools if tool_surface_source_tools is not None else tools)
        caller_non_namespace_tools = tuple(
            tool
            for tool in surface_source_tools
            if not (isinstance(tool, Mapping) and tool.get("type") == "namespace")
        )
        namespace_declaration_count = sum(
            1 for tool in surface_source_tools if self.is_flattened_namespace_schema(tool)
        )
        flattened_namespace_tools = self.flatten_namespace_function_tools(surface_source_tools)
        if strip_namespace_tools:
            namespace_to_strip = (
                self.is_raw_namespace_schema if strip_all_namespace_tools else self.is_flattened_namespace_schema
            )
            filtered_tools = [tool for tool in tools if not namespace_to_strip(tool)]
            if len(filtered_tools) != len(tools):
                tools[:] = filtered_tools
                changed = True

        if not include_local_tool_gateway_tools:
            filtered_tools = [tool for tool in tools if not self.is_local_tool_gateway_tool_schema(tool)]
            if len(filtered_tools) != len(tools):
                tools[:] = filtered_tools
                changed = True
            flattened_namespace_tools = [
                tool for tool in flattened_namespace_tools if not self.is_local_tool_gateway_tool_schema(tool)
            ]

        if not include_multi_agent_tools:
            filtered_tools = [tool for tool in tools if not self.is_multi_agent_tool_schema(tool)]
            if len(filtered_tools) != len(tools):
                tools[:] = filtered_tools
                changed = True

        if not include_node_repl_tools:
            filtered_tools = [tool for tool in tools if not self.is_node_repl_tool_schema(tool)]
            if len(filtered_tools) != len(tools):
                tools[:] = filtered_tools
                changed = True

        excluded_tool_names = set()
        search_name = self.facts.tool_search_explicit_function_tool["name"]
        if not include_tool_search:
            excluded_tool_names.add(search_name)
        if not include_multi_agent_tools:
            excluded_tool_names.update(
                f"multi_agent_v1__{tool_name}" for tool_name in self.facts.multi_agent_tool_names
            )
        if not include_spawn_agent:
            excluded_tool_names.add("multi_agent_v1__spawn_agent")
        if not include_wait_agent:
            excluded_tool_names.add("multi_agent_v1__wait_agent")
        if not include_close_agent:
            excluded_tool_names.add("multi_agent_v1__close_agent")
        if not include_resume_agent:
            excluded_tool_names.add("multi_agent_v1__resume_agent")
        if not include_send_input:
            excluded_tool_names.add("multi_agent_v1__send_input")
        if excluded_tool_names:
            filtered_tools = [
                tool
                for tool in tools
                if not (
                    isinstance(tool, Mapping)
                    and tool.get("type") == "function"
                    and tool.get("name") in excluded_tool_names
                )
            ]
            if len(filtered_tools) != len(tools):
                tools[:] = filtered_tools
                changed = True

        existing_names = {self.tool_schema_name(tool) for tool in tools}
        existing_names.discard(None)
        core_additions = []
        if include_tool_search:
            core_additions.append(self.facts.tool_search_explicit_function_tool)
        if include_multi_agent_tools:
            core_additions.extend(
                self.multi_agent_explicit_function_tools(
                    include_spawn_agent=include_spawn_agent,
                    include_wait_agent=include_wait_agent,
                    include_close_agent=include_close_agent,
                    include_resume_agent=include_resume_agent,
                    include_send_input=include_send_input,
                    open_agent_ids=open_agent_ids,
                    wait_agent_ids=wait_agent_ids,
                    close_agent_ids=close_agent_ids,
                    worker_selector_values=worker_selector_values,
                )
            )
        if not include_multi_agent_tools:
            core_additions = [tool for tool in core_additions if not self.is_multi_agent_tool_schema(tool)]
            flattened_namespace_tools = [
                tool for tool in flattened_namespace_tools if not self.is_multi_agent_tool_schema(tool)
            ]
        if not include_node_repl_tools:
            core_additions = [tool for tool in core_additions if not self.is_node_repl_tool_schema(tool)]
            flattened_namespace_tools = [
                tool for tool in flattened_namespace_tools if not self.is_node_repl_tool_schema(tool)
            ]
        if excluded_tool_names:
            core_additions = [
                tool
                for tool in core_additions
                if not (
                    isinstance(tool, Mapping)
                    and tool.get("type") == "function"
                    and tool.get("name") in excluded_tool_names
                )
            ]
            flattened_namespace_tools = [
                tool
                for tool in flattened_namespace_tools
                if not (
                    isinstance(tool, Mapping)
                    and tool.get("type") == "function"
                    and tool.get("name") in excluded_tool_names
                )
            ]

        potential_names = set(existing_names)
        for tool in core_additions:
            name = self.tool_schema_name(tool)
            if name:
                potential_names.add(name)
        deferred_tool_count = 0
        for tool in flattened_namespace_tools:
            name = self.tool_schema_name(tool)
            if name and name not in potential_names:
                potential_names.add(name)
                deferred_tool_count += 1

        flattened_tool_ids = {id(tool) for tool in flattened_namespace_tools}
        additions = list(core_additions)
        if include_flattened_namespace_tools:
            additions.extend(flattened_namespace_tools)

        eager_tool_count = 0
        for tool in additions:
            name = self.tool_schema_name(tool)
            if not name:
                continue
            replaced_existing = False
            if name in existing_names:
                for index, existing_tool in enumerate(tools):
                    if not isinstance(existing_tool, Mapping) or self.tool_schema_name(existing_tool) != name:
                        continue
                    if name.startswith("multi_agent_v1__") and dict(existing_tool) != tool:
                        tools[index] = tool
                        changed = True
                    replaced_existing = True
                    break
            if replaced_existing:
                continue
            tools.append(tool)
            existing_names.add(name)
            if id(tool) in flattened_tool_ids:
                eager_tool_count += 1
            changed = True
        if tool_surface_counts is not None:
            if deferred_core_surface:
                namespace_declaration_count, deferred_tool_count = self.deferred_namespace_surface_counts(
                    surface_source_tools,
                    tools,
                )
            surviving_tool_ids = {id(tool) for tool in tools}
            tool_surface_counts.update(
                {
                    "namespace_declaration_count": namespace_declaration_count,
                    "eager_tool_count": eager_tool_count if include_flattened_namespace_tools else 0,
                    "retained_core_count": sum(
                        1 for tool in caller_non_namespace_tools if id(tool) in surviving_tool_ids
                    ),
                    "deferred_tool_count": deferred_tool_count if not include_flattened_namespace_tools else 0,
                }
            )
        return changed

    def restore_deferred_core_node_repl_namespace(
        self,
        payload: dict[str, Any],
        source_tools: list[Any] | None,
    ) -> bool:
        tools = payload.get("tools")
        if not isinstance(tools, list) or not isinstance(source_tools, list):
            return False
        if any(self.is_node_repl_tool_schema(tool) for tool in tools):
            return False
        namespaces = [
            tool
            for tool in source_tools
            if isinstance(tool, Mapping)
            and tool.get("type") == "namespace"
            and tool.get("name") == self.facts.node_repl_namespace
        ]
        if len(namespaces) != 1:
            return False
        source_namespace = namespaces[0]
        children = source_namespace.get("tools")
        if not isinstance(children, list):
            return False
        js_children = [
            child
            for child in children
            if isinstance(child, Mapping)
            and child.get("type") == "function"
            and child.get("name") == "js"
        ]
        if len(js_children) != 1:
            return False
        tools.append({**source_namespace, "tools": [js_children[0]]})
        return True

    def structured_tool_function_call_item(self, item: Mapping[str, Any]) -> dict[str, Any] | None:
        if item.get("type") != "function_call":
            return None
        request_shape = dict(item)
        for response_only_field in ("id", "status", self.facts.worker_requested_binding_field):
            request_shape.pop(response_only_field, None)
        if item.get("name") == self.facts.apply_patch_function_name and isinstance(item.get("id"), str):
            request_shape["id"] = item["id"]
        tool_name = self.multi_agent_function_call_name(item)
        if tool_name is not None:
            rewritten = request_shape
            rewritten.pop("namespace", None)
            rewritten["name"] = f"multi_agent_v1__{tool_name}"
            normalized, _, args_changed = normalize_multi_agent_arguments(rewritten.get("arguments"), tool_name)
            if args_changed:
                rewritten["arguments"] = normalized
            return rewritten
        node_name = self.node_repl_function_call_name(item)
        if node_name is not None:
            rewritten = request_shape
            rewritten.pop("namespace", None)
            rewritten["name"] = f"{self.facts.node_repl_namespace}__{node_name}"
            return rewritten
        return request_shape

    def same_selected_v1_collaboration_function_call(
        self,
        item: Mapping[str, Any],
        event_context: Mapping[str, Any] | None,
    ) -> bool:
        if (
            item.get("type") != "function_call"
            or not isinstance(event_context, Mapping)
            or event_context.get("collaboration_protocol") != self.facts.collaboration_v1
        ):
            return False
        try:
            return classify_collaboration_payload({"input": [item]}) == self.facts.collaboration_v1
        except CollaborationBoundaryError:
            return False

    def runtime_plan_has_native_plain_function(
        self,
        plan: ToolCompatibilityPlanView | None,
        item: Mapping[str, Any],
    ) -> bool:
        name = item.get("name")
        return bool(
            plan is not None
            and isinstance(name, str)
            and any(
                entry.family == "plain_function"
                and entry.disposition == "native"
                and entry.original_name == name
                for entry in plan.entries
            )
        )

    def hoist_additional_tools_input_items(self, payload: dict[str, Any]) -> bool:
        input_items = payload.get("input")
        if not isinstance(input_items, list):
            return False

        promoted_tools: list[Any] = []
        rewritten_items: list[Any] = []
        changed = False
        for item in input_items:
            if not isinstance(item, Mapping) or item.get("type") != "additional_tools":
                rewritten_items.append(item)
                continue
            item_tools = item.get("tools")
            if isinstance(item_tools, list):
                promoted_tools.extend(item_tools)
            changed = True

        if not changed:
            return False

        tools = payload.get("tools")
        if isinstance(tools, list):
            tools.extend(promoted_tools)
        elif tools is None:
            payload["tools"] = promoted_tools
        else:
            payload["tools"] = promoted_tools
        payload["input"] = rewritten_items
        return True

    def rewrite_structured_tool_input_items(
        self,
        payload: dict[str, Any],
        event_context: Mapping[str, Any] | None = None,
        compatibility_plan: ToolCompatibilityPlanView | None = None,
    ) -> bool:
        input_items = payload.get("input")
        if not isinstance(input_items, list):
            return False

        input_items, adapted_apply_patch_call_ids, changed = self.adapt_apply_patch_history(
            input_items,
            event_context=event_context,
        )
        if changed:
            payload["input"] = input_items
        rewritten_items: list[Any] = []
        preserved_structured_call_ids: set[str] = set(adapted_apply_patch_call_ids)
        available_function_names = self.function_tool_names(payload.get("tools"))
        apply_patch_name = self.facts.apply_patch_function_name
        for item in input_items:
            if not isinstance(item, dict):
                rewritten_items.append(item)
                continue
            if (
                compatibility_plan is not None
                and compatibility_plan.owns_wire_value(item)
                and not self.same_selected_v1_collaboration_function_call(item, event_context)
                and not self.runtime_plan_has_native_plain_function(compatibility_plan, item)
            ):
                call_id = item.get("call_id")
                if isinstance(call_id, str):
                    preserved_structured_call_ids.add(call_id)
                rewritten_items.append(item)
                continue
            if item.get("type") == "function_call":
                function_name = item.get("name")
                call_id = item.get("call_id")
                preserve_apply_patch_history = (
                    function_name == apply_patch_name
                    and isinstance(call_id, str)
                    and call_id in adapted_apply_patch_call_ids
                )
                preserve_available_function = (
                    isinstance(function_name, str) and function_name in available_function_names
                )
                if (
                    preserve_available_function
                    or preserve_apply_patch_history
                    or self.multi_agent_function_call_name(item) is not None
                    or self.node_repl_function_call_name(item) is not None
                ):
                    if isinstance(call_id, str):
                        preserved_structured_call_ids.add(call_id)
                    rewritten = self.structured_tool_function_call_item(item)
                    rewritten_items.append(rewritten if rewritten is not None else item)
                    changed = changed or rewritten != item
                else:
                    replacement = self.compatible_internal_message(item)
                    if replacement is not None:
                        rewritten_items.append(replacement)
                    changed = True
                continue
            if item.get("type") == "function_call_output":
                call_id = item.get("call_id")
                if isinstance(call_id, str) and call_id in preserved_structured_call_ids:
                    rewritten_items.append(dict(item))
                else:
                    replacement = self.compatible_internal_message(item)
                    if replacement is not None:
                        rewritten_items.append(replacement)
                    changed = True
                continue
            item_type = item.get("type")
            replacement = self.compatible_internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
            elif isinstance(item_type, str) and item_type in self.facts.internal_input_item_types:
                changed = True
            else:
                rewritten_items.append(item)

        if changed:
            payload["input"] = rewritten_items
        return changed

    def is_multi_agent_discovery_arguments(self, arguments: Mapping[str, Any] | None) -> bool:
        if not arguments:
            return False
        query = arguments.get("query")
        if not isinstance(query, str):
            return False
        lowered = query.lower()
        return all(term in lowered for term in ("spawn_agent", "multi_agent", "subagent"))

    def bounded_empty_tool_search_terminal_calls(self, value: Any) -> dict[str, tuple[str, int]]:
        if not isinstance(value, list):
            return {}

        queries_by_call_id: dict[str, str] = {}
        empty_call_ids_by_query: dict[str, list[str]] = {}
        successful_queries: set[str] = set()
        miss_bound = self.facts.tool_search_empty_miss_bound
        for item in value:
            if not isinstance(item, Mapping):
                continue
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if item.get("type") == "tool_search_call" and item.get("execution") == "client":
                arguments = normalize_tool_search_arguments(item.get("arguments"))
                if arguments is None or self.is_multi_agent_discovery_arguments(arguments):
                    continue
                queries_by_call_id[call_id] = arguments["query"]
                continue
            if item.get("type") != "tool_search_output":
                continue
            query = queries_by_call_id.pop(call_id, None)
            tools = item.get("tools")
            if query is None or not isinstance(tools, list):
                continue
            if tools:
                successful_queries.add(query)
                continue
            empty_call_ids_by_query.setdefault(query, []).append(call_id)

        terminal_calls: dict[str, tuple[str, int]] = {}
        for query, call_ids in empty_call_ids_by_query.items():
            if query in successful_queries or len(call_ids) < miss_bound:
                continue
            terminal_calls[call_ids[miss_bound - 1]] = (query, miss_bound)
        return terminal_calls

    def terminalize_bounded_empty_tool_search_misses(
        self,
        payload: dict[str, Any],
        terminal_calls: Mapping[str, tuple[str, int]],
    ) -> bool:
        input_items = payload.get("input")
        if not isinstance(input_items, list) or not terminal_calls:
            return False

        rewritten_items: list[Any] = []
        changed = False
        for item in input_items:
            if (
                isinstance(item, Mapping)
                and item.get("type") == "tool_search_output"
                and isinstance(item.get("call_id"), str)
                and item["call_id"] in terminal_calls
            ):
                rewritten = dict(item)
                rewritten["status"] = self.facts.tool_search_unavailable_status
                rewritten["query_classification"] = self.facts.tool_search_unavailable_query_classification
                _, count = terminal_calls[item["call_id"]]
                rewritten["empty_miss_count"] = count
                rewritten["terminal"] = True
                rewritten_items.append(rewritten)
                changed = True
                continue
            rewritten_items.append(item)
        if changed:
            payload["input"] = rewritten_items
        return changed

    def restrict_bounded_tool_search_queries(self, payload: dict[str, Any], bounded_queries: set[str]) -> bool:
        tools = payload.get("tools")
        if not isinstance(tools, list) or not bounded_queries:
            return False

        restriction = {"enum": sorted(bounded_queries)}
        changed = False
        rewritten_tools: list[Any] = []
        search_name = self.facts.tool_search_explicit_function_tool["name"]
        for tool in tools:
            if not (
                isinstance(tool, Mapping)
                and tool.get("type") == "function"
                and tool.get("name") == search_name
            ):
                rewritten_tools.append(tool)
                continue
            rewritten_tool = dict(tool)
            parameters = dict(self.tool_parameters_schema(tool))
            properties_value = parameters.get("properties")
            properties = dict(properties_value) if isinstance(properties_value, Mapping) else {}
            query_value = properties.get("query")
            query_schema = dict(query_value) if isinstance(query_value, Mapping) else {"type": "string"}
            if "not" in query_schema:
                query_schema = {"allOf": [query_schema, {"not": restriction}]}
            else:
                query_schema["not"] = restriction
            properties["query"] = query_schema
            parameters["properties"] = properties
            rewritten_tool["parameters"] = parameters
            rewritten_tools.append(rewritten_tool)
            changed = True
        if changed:
            payload["tools"] = rewritten_tools
        return changed

    def tool_search_query_digest(self, query: str) -> bytes:
        return hashlib.sha256(query.encode("utf-8")).digest()

    def bounded_tool_search_query_digests(self, event_context: Mapping[str, Any] | None) -> set[bytes]:
        value = (event_context or {}).get("_bounded_tool_search_query_digests")
        if not isinstance(value, (set, frozenset)):
            return set()
        return {digest for digest in value if isinstance(digest, bytes)}

    def tool_search_call_arguments(
        self,
        value: Mapping[str, Any],
        *,
        candidate_item_ids: set[str] | None = None,
        allow_legacy_function: bool = False,
    ) -> dict[str, Any] | None:
        if value.get("type") == "tool_search_call" and value.get("execution") == "client":
            return normalize_tool_search_arguments(value.get("arguments"))
        if (
            value.get("type") == "function_call"
            and value.get("name") == "tool_search"
            and isinstance(value.get("id"), str)
            and value.get("id")
            and candidate_item_ids is not None
            and value.get("id") in candidate_item_ids
            and allow_legacy_function
        ):
            return normalize_tool_search_arguments(value.get("arguments"))
        return None

    def bounded_tool_search_unavailable_message(self, item: Mapping[str, Any]) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": (
                        "tool_search_unavailable\n"
                        f"query_classification: {self.facts.tool_search_unavailable_query_classification}\n"
                        f"empty_miss_count: {self.facts.tool_search_empty_miss_bound}\n"
                        f"status: {self.facts.tool_search_unavailable_status}\n"
                        "terminal: true\n"
                        "execution: suppressed"
                    ),
                }
            ],
        }
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            message["id"] = item_id
        return message

    def suppress_bounded_tool_search_calls(
        self,
        value: Any,
        event_context: Mapping[str, Any] | None,
    ) -> tuple[Any, bool]:
        bounded_digests = self.bounded_tool_search_query_digests(event_context)
        if not bounded_digests:
            return value, False

        if isinstance(event_context, dict):
            candidates_value = event_context.setdefault("_tool_search_stream_candidate_item_ids", set())
            candidate_item_ids = candidates_value if isinstance(candidates_value, set) else set()
            event_context["_tool_search_stream_candidate_item_ids"] = candidate_item_ids
            suppressed_value = event_context.setdefault("_bounded_tool_search_suppressed_item_ids", set())
            suppressed_item_ids = suppressed_value if isinstance(suppressed_value, set) else set()
            event_context["_bounded_tool_search_suppressed_item_ids"] = suppressed_item_ids
            allow_legacy_function = bool(event_context.get("_tool_search_client_owned"))
        else:
            candidate_item_ids = set()
            suppressed_item_ids = set()
            allow_legacy_function = False

        return self._suppress_bounded_tool_search_calls_inner(
            value,
            bounded_digests,
            candidate_item_ids,
            suppressed_item_ids,
            allow_legacy_function,
        )

    def _suppress_bounded_tool_search_calls_inner(
        self,
        value: Any,
        bounded_digests: set[bytes],
        candidate_item_ids: set[str],
        suppressed_item_ids: set[str],
        allow_legacy_function: bool,
    ) -> tuple[Any, bool]:
        if isinstance(value, list):
            changed = False
            rewritten_items: list[Any] = []
            for item in value:
                replacement, item_changed = self._suppress_bounded_tool_search_calls_inner(
                    item,
                    bounded_digests,
                    candidate_item_ids,
                    suppressed_item_ids,
                    allow_legacy_function,
                )
                if replacement is None:
                    changed = True
                    continue
                rewritten_items.append(replacement)
                changed = changed or item_changed
            return (rewritten_items if changed else value), changed

        if not isinstance(value, dict):
            return value, False

        event_type = value.get("type")
        if event_type == "response.output_item.added":
            item = value.get("item")
            if isinstance(item, Mapping):
                item_id = item.get("id")
                if (
                    isinstance(item_id, str)
                    and item_id
                    and (
                        (
                            item.get("type") == "tool_search_call"
                            and item.get("execution") == "client"
                        )
                        or (
                            allow_legacy_function
                            and item.get("type") == "function_call"
                            and item.get("name") == "tool_search"
                        )
                    )
                ):
                    candidate_item_ids.add(item_id)
        elif event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            item_id = value.get("item_id")
            if isinstance(item_id, str) and item_id in suppressed_item_ids:
                return None, True
            if (
                event_type == "response.function_call_arguments.done"
                and isinstance(item_id, str)
                and item_id in candidate_item_ids
            ):
                arguments = normalize_tool_search_arguments(value.get("arguments"))
                if (
                    arguments is not None
                    and self.tool_search_query_digest(arguments["query"]) in bounded_digests
                ):
                    suppressed_item_ids.add(item_id)
                    return None, True

        arguments = self.tool_search_call_arguments(
            value,
            candidate_item_ids=candidate_item_ids,
            allow_legacy_function=allow_legacy_function,
        )
        if (
            arguments is not None
            and self.tool_search_query_digest(arguments["query"]) in bounded_digests
        ):
            item_id = value.get("id")
            if isinstance(item_id, str) and item_id:
                suppressed_item_ids.add(item_id)
            return self.bounded_tool_search_unavailable_message(value), True

        changed = False
        rewritten = dict(value)
        for key, item in value.items():
            replacement, item_changed = self._suppress_bounded_tool_search_calls_inner(
                item,
                bounded_digests,
                candidate_item_ids,
                suppressed_item_ids,
                allow_legacy_function,
            )
            if replacement is None:
                rewritten.pop(key, None)
                changed = True
                continue
            if item_changed:
                rewritten[key] = replacement
                changed = True
        return (rewritten if changed else value), changed

    def is_collaboration_v2_context(self, event_context: Mapping[str, Any] | None) -> bool:
        return (event_context or {}).get("collaboration_protocol") == self.facts.collaboration_v2

    def normalize_third_party_tool_call(
        self,
        value: Any,
        event_context: Mapping[str, Any] | None = None,
    ) -> tuple[Any, bool]:
        if self.is_collaboration_v2_context(event_context):
            return value, False
        if isinstance(value, list):
            changed = False
            rewritten = []
            for item in value:
                replacement, item_changed = self.normalize_third_party_tool_call(item, event_context)
                rewritten.append(replacement)
                changed = changed or item_changed
            return (rewritten if changed else value), changed

        if not isinstance(value, dict):
            return value, False

        changed = False
        rewritten = dict(value)
        node_repl = self.facts.node_repl_namespace
        apply_patch_name = self.facts.apply_patch_function_name
        discovery_arguments = multi_agent_discovery_arguments(value.get("arguments"))
        if (
            value.get("type") == "function_call"
            and value.get("name") == "tool_search"
            and bool((event_context or {}).get("_tool_search_client_owned"))
        ):
            arguments = normalize_tool_search_arguments(value.get("arguments"))
            if arguments is not None:
                rewritten["type"] = "tool_search_call"
                rewritten["arguments"] = arguments
                rewritten.pop("name", None)
                rewritten.setdefault("execution", "client")
                rewritten.setdefault("status", "completed")
                changed = True
        elif (
            value.get("type") == "function_call"
            and value.get("name") in self.facts.multi_agent_namespace_aliases
            and discovery_arguments is not None
        ):
            arguments = discovery_arguments
            rewritten["type"] = "tool_search_call"
            rewritten["arguments"] = arguments
            rewritten.pop("name", None)
            rewritten.setdefault("execution", "client")
            rewritten.setdefault("status", "completed")
            changed = True
        elif self.is_tool_call_item(value):
            original_name = value.get("name")
            tool_name = self.multi_agent_alias_tool_name(original_name)
            namespace_alias = None
            argument_key = "arguments" if "arguments" in value else "input" if "input" in value else None
            if (
                argument_key is not None
                and not (
                    value.get("type") == "custom_tool_call"
                    and original_name == apply_patch_name
                )
                and json_argument_string_needs_repair(value.get(argument_key))
            ):
                repaired_arguments = json_object_from_arguments(value.get(argument_key))
                if repaired_arguments is not None:
                    rewritten[argument_key] = dump_arguments_like(value.get(argument_key), repaired_arguments)
                    changed = True
            if (
                (value.get("namespace") == node_repl and original_name == "js")
                or original_name in {f"{node_repl}.js", f"{node_repl}__js"}
            ):
                rewritten["namespace"] = node_repl
                rewritten["name"] = "js"
                changed = True
            elif tool_name is None:
                namespace_alias = self.split_namespace_tool_alias(original_name)
            if original_name in self.facts.multi_agent_namespace_aliases and argument_key is not None:
                normalized, tool_name, args_changed = normalize_multi_agent_arguments(rewritten.get(argument_key), None)
                if args_changed:
                    rewritten[argument_key] = normalized
                    changed = True
            elif tool_name is not None and argument_key is not None:
                normalized, _, args_changed = normalize_multi_agent_arguments(rewritten.get(argument_key), tool_name)
                if args_changed:
                    rewritten[argument_key] = normalized
                    changed = True

            if tool_name is not None:
                rewritten["name"] = tool_name
                rewritten["namespace"] = "multi_agent_v1"
                changed = True
            elif namespace_alias is not None:
                namespace_name, namespaced_tool_name = namespace_alias
                rewritten["name"] = namespaced_tool_name
                rewritten["namespace"] = namespace_name
                changed = True
        else:
            original_name = value.get("name")
            tool_name = self.multi_agent_alias_tool_name(original_name)
            if tool_name is not None and self.looks_like_response_tool_name_fragment(value):
                rewritten["name"] = tool_name
                rewritten["namespace"] = "multi_agent_v1"
                changed = True

        for key, item in list(rewritten.items()):
            replacement, item_changed = self.normalize_third_party_tool_call(item, event_context)
            if item_changed:
                rewritten[key] = replacement
                changed = True

        return (rewritten if changed else value), changed

    def downgrade_invalid_third_party_tool_calls(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, list):
            changed = False
            rewritten = []
            for item in value:
                replacement, item_changed = self.downgrade_invalid_third_party_tool_calls(item)
                rewritten.append(replacement)
                changed = changed or item_changed
            return (rewritten if changed else value), changed

        if not isinstance(value, dict):
            return value, False

        if self.has_invalid_tool_name(value):
            title = (
                "Invalid third-party tool call transcript"
                if value.get("type") == "custom_tool_call"
                else "Invalid third-party function call transcript"
            )
            return self.transcript_message(title, value), True

        changed = False
        rewritten = dict(value)
        for key, item in value.items():
            replacement, item_changed = self.downgrade_invalid_third_party_tool_calls(item)
            if item_changed:
                rewritten[key] = replacement
                changed = True
        return (rewritten if changed else value), changed
