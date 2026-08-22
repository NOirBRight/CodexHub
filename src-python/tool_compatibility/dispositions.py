"""Declaration families, hosted kinds, and the four compatibility dispositions."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from collaboration_runtime_contract import (
    COLLABORATION_V1,
    COLLABORATION_V2,
    CollaborationContractError,
    classify_collaboration_request,
)

from .contracts import (
    ProtocolCapabilities,
    RequiredToolUnavailableError,
    ToolCompatibilityError,
    _MalformedDeclaration,
    _copy_mapping,
    _freeze,
    _protocol_capabilities,
    _provider_hosted,
)


NATIVE = "native"
ADAPT = "adapt"
OMIT = "omit"
REQUIRED_BUT_UNAVAILABLE = "required-but-unavailable"

PLAIN_FUNCTION = "plain_function"
NAMESPACE = "namespace"
CUSTOM_FREEFORM = "custom_freeform"
TOOL_SEARCH = "tool_search"
SELECTED_PROVIDER_HOSTED = "selected_provider_hosted"
UNKNOWN_FUTURE_KIND = "unknown_future_kind"

_KNOWN_HOSTED_TYPES = frozenset(
    {
        "web_search",
        "web_search_preview",
        "file_search",
        "computer_use_preview",
        "code_interpreter",
        "local_shell",
    }
)
# Only these hosted tools have a complete, bounded Responses lifecycle we can
# validate.  A provider capability fact alone is not enough to make a hosted
# kind native: without this map we would have to guess its event names/order.
_HOSTED_EVENT_STAGES = {
    "web_search": ("web_search_call", ("in_progress", "searching", "completed")),
    "web_search_preview": ("web_search_call", ("in_progress", "searching", "completed")),
    "file_search": ("file_search_call", ("in_progress", "searching", "completed")),
    "code_interpreter": ("code_interpreter_call", ("in_progress", "interpreting", "completed")),
}


def _name_of(value: Mapping[str, Any]) -> str | None:
    name = value.get("name")
    if isinstance(name, str) and name:
        return name
    function = value.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _is_explicit_function_tool_search(declaration: Mapping[str, Any]) -> bool:
    if declaration.get("type") != "function" or declaration.get("name") != "tool_search":
        return False
    if declaration.get("execution") != "client":
        return False
    if declaration.get("description") != "Discover deferred Codex tools by keyword. Use this before calling a tool that is not already visible.":
        return False
    parameters = declaration.get("parameters")
    required = parameters.get("required") if isinstance(parameters, Mapping) else None
    return (
        isinstance(parameters, Mapping)
        and parameters.get("type") == "object"
        and isinstance(required, (list, tuple))
        and tuple(required) == ("query",)
        and parameters.get("additionalProperties") is False
    )


def _namespace_details(declaration: Mapping[str, Any]) -> tuple[str | None, tuple[Mapping[str, Any], ...], str | None, bool]:
    namespace = declaration.get("name")
    namespace = namespace if isinstance(namespace, str) and namespace else None
    raw_tools = declaration.get("tools")
    if not isinstance(raw_tools, list):
        return namespace, (), None, False
    children: list[Mapping[str, Any]] = []
    for child in raw_tools:
        if not isinstance(child, Mapping) or child.get("type") != "function" or not _name_of(child):
            return namespace, (), None, False
        children.append(child)
    child_names = [str(child.get("name")) for child in children]
    if len(set(child_names)) != len(child_names):
        return namespace, (), None, False
    if namespace == "multi_agent_v1":
        version = "v1"
    elif namespace == "collaboration":
        version = "v2"
    else:
        version = None
    return namespace, tuple(children), version, bool(namespace and children)


def classify_declaration(declaration: Mapping[str, Any]) -> str:
    item_type = declaration.get("type")
    if item_type == "namespace":
        return NAMESPACE
    if item_type == "function":
        # A function-shaped discovery entry is only client-owned when the
        # explicit execution marker is present. A provider function that merely
        # happens to be named ``tool_search`` must remain an ordinary function;
        # otherwise the Gateway would silently take ownership of its lifecycle.
        if _is_explicit_function_tool_search(declaration):
            return TOOL_SEARCH
        return PLAIN_FUNCTION
    if item_type == "custom":
        return CUSTOM_FREEFORM
    if item_type == "tool_search":
        return TOOL_SEARCH
    if isinstance(item_type, str) and item_type in _KNOWN_HOSTED_TYPES:
        return SELECTED_PROVIDER_HOSTED
    return UNKNOWN_FUTURE_KIND


def _declaration_valid_for_family(declaration: Mapping[str, Any], family: str) -> bool:
    if family == PLAIN_FUNCTION:
        return declaration.get("type") == "function" and bool(_name_of(declaration))
    if family == NAMESPACE:
        return _namespace_details(declaration)[3]
    if family == CUSTOM_FREEFORM:
        return (
            declaration.get("type") == "custom"
            and bool(_name_of(declaration))
            and isinstance(declaration.get("format"), Mapping)
        )
    if family == TOOL_SEARCH:
        return (
            (
                declaration.get("type") == "tool_search"
                and declaration.get("execution") == "client"
            )
            or (
                _is_explicit_function_tool_search(declaration)
            )
        )
    if family == SELECTED_PROVIDER_HOSTED:
        return isinstance(declaration.get("type"), str)
    return isinstance(declaration.get("type"), str)


def _hosted_kind_for_item_type(item_type: Any) -> str | None:
    if not isinstance(item_type, str) or not item_type.endswith("_call"):
        return None
    kind = item_type[: -len("_call")]
    return kind if kind in _KNOWN_HOSTED_TYPES else None


def _hosted_output_kind_for_item_type(item_type: Any) -> str | None:
    if not isinstance(item_type, str):
        return None
    for event_kind, _stages in _HOSTED_EVENT_STAGES.values():
        if item_type == f"{event_kind}_output":
            return event_kind
    return None


def _hosted_event_spec_for_declaration_kind(kind: Any) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(kind, str):
        return None
    return _HOSTED_EVENT_STAGES.get(kind)


def _hosted_event_spec_for_item_type(item_type: Any) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(item_type, str):
        return None
    for event_kind, stages in _HOSTED_EVENT_STAGES.values():
        if item_type in {event_kind, f"{event_kind}_output"}:
            return event_kind, stages
    return None


def _hosted_history_item_key(item_type: Any) -> tuple[str, bool] | None:
    if not isinstance(item_type, str):
        return None
    if item_type in {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "tool_search_call",
        "tool_search_output",
    }:
        return None
    for event_kind, _stages in _HOSTED_EVENT_STAGES.values():
        if item_type == event_kind:
            return event_kind, False
        if item_type == f"{event_kind}_output":
            return event_kind, True
    for hosted_kind in _KNOWN_HOSTED_TYPES:
        if item_type == f"{hosted_kind}_call":
            return hosted_kind, False
        if item_type == f"{hosted_kind}_call_output":
            return hosted_kind, True
    return None


def _hosted_event_spec(event_type: Any) -> tuple[str, str, tuple[str, ...]] | None:
    if not isinstance(event_type, str) or not event_type.startswith("response."):
        return None
    suffix = event_type[len("response.") :]
    event_kind, separator, stage = suffix.rpartition(".")
    if not separator:
        return None
    for mapped_event_kind, stages in _HOSTED_EVENT_STAGES.values():
        if event_kind == mapped_event_kind and stage in stages:
            return event_kind, stage, stages
    return None


def _is_unsupported_hosted_stream_event(event_type: Any) -> bool:
    if not isinstance(event_type, str) or not event_type.startswith("response."):
        return False
    if _hosted_event_spec(event_type) is not None:
        return False
    suffix = event_type[len("response.") :]
    # These are the Responses protocol's ordinary function/custom/tool-search
    # lifecycles.  They are handled by the request-scoped stream ledger below;
    # the ``*_call`` spelling alone is not enough to classify an event as a
    # provider-hosted lifecycle.
    if suffix.startswith(
        (
            "function_call_arguments.",
            "custom_tool_call_input.",
            "tool_search_call.",
        )
    ):
        return False
    # Any remaining ``<kind>_call.<event>`` is either a known hosted kind or an
    # unknown provider-specific hosted lifecycle.  Neither can be safely
    # passed through without an explicit lifecycle contract.
    return "_call." in suffix


def _tool_choice_matches_declaration(
    declaration: Mapping[str, Any],
    tool_choice: Any,
) -> bool:
    """Return whether an explicit choice identifies this declaration exactly."""
    family = classify_declaration(declaration)
    name = _name_of(declaration)
    namespace, children, _version, _valid = _namespace_details(declaration)

    if isinstance(tool_choice, str):
        if tool_choice in {"auto", "none", "required"}:
            return False
        if family == NAMESPACE:
            return tool_choice == namespace or any(
                tool_choice == f"{namespace}__{child.get('name')}" for child in children
            )
        if family == TOOL_SEARCH and tool_choice == "tool_search":
            return True
        return tool_choice == name

    if not isinstance(tool_choice, Mapping):
        return False
    choice_type = tool_choice.get("type")
    choice_name = tool_choice.get("name")
    if choice_type in (None, "function"):
        if not isinstance(choice_name, str):
            return False
        if family == PLAIN_FUNCTION:
            return choice_name == name
        if family == TOOL_SEARCH and choice_name == "tool_search":
            return True
        if family == NAMESPACE:
            choice_namespace = tool_choice.get("namespace")
            return (
                choice_namespace == namespace and choice_name in {child.get("name") for child in children}
            ) or (
                choice_namespace is None
                and any(choice_name == f"{namespace}__{child.get('name')}" for child in children)
            )
        return False
    if choice_type == "namespace":
        return family == NAMESPACE and choice_name in {None, namespace}
    if choice_type == "custom":
        return family == CUSTOM_FREEFORM and choice_name == name
    if choice_type == "tool_search":
        return family == TOOL_SEARCH
    if isinstance(choice_type, str) and choice_type == declaration.get("type"):
        return choice_name in {None, name}
    return False


def _has_explicit_named_tool_choice(tool_choice: Any) -> bool:
    if isinstance(tool_choice, str):
        return tool_choice not in {"auto", "none", "required"}
    if not isinstance(tool_choice, Mapping):
        return False
    if isinstance(tool_choice.get("name"), str):
        return True
    return tool_choice.get("type") in {
        "namespace",
        "custom",
        "tool_search",
        *_KNOWN_HOSTED_TYPES,
    }


def _required_by_rule(
    declaration: Mapping[str, Any],
    index: int,
    *,
    required: Any,
    tool_choice: Any,
) -> bool:
    names = {_name_of(declaration), declaration.get("type"), declaration.get("name")}
    names.discard(None)
    namespace, children, _version, valid = _namespace_details(declaration)
    if namespace:
        names.add(namespace)
        names.update(child.get("name") for child in children if isinstance(child.get("name"), str))

    result = False
    if isinstance(required, bool):
        result = required
    elif isinstance(required, Mapping):
        for key in (index, str(index), declaration.get("type"), declaration.get("name"), namespace):
            if key in required and isinstance(required[key], bool):
                result = result or required[key]
        for key, enabled in required.items():
            if enabled is True and key in names:
                result = True
    elif isinstance(required, str):
        result = required in names
    elif isinstance(required, Iterable):
        required_values = {str(item) for item in required}
        result = bool(required_values.intersection({str(item) for item in names}))

    if tool_choice in ("required", True):
        return result
    if not isinstance(tool_choice, Mapping):
        if isinstance(tool_choice, str) and tool_choice not in {"auto", "none"}:
            return result or _tool_choice_matches_declaration(declaration, tool_choice)
        return result
    return result or _tool_choice_matches_declaration(declaration, tool_choice)



def build_tool_compatibility_plan(
    runtime_declarations: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    selected_protocol: str,
    provider_hosted_capabilities: Any = None,
    required: Any = None,
    tool_choice: Any = None,
    protocol_capabilities: ProtocolCapabilities | Mapping[str, Any] | None = None,
    request_token: str = "request",
    native_names: Iterable[str] = (),
    collaboration_protocol: str | None = None,
) -> "ToolCompatibilityPlan":
    from .plan import ToolCompatibilityEntry, ToolCompatibilityPlan
    from .registry import CompatibilityDiagnostics, RequestScopedToolAliasRegistry

    if isinstance(runtime_declarations, Mapping):
        candidate = runtime_declarations.get("tools", ())
    else:
        candidate = runtime_declarations
    if isinstance(candidate, (str, bytes, bytearray)) or not isinstance(candidate, Iterable):
        raise _MalformedDeclaration()
    declarations: list[Mapping[str, Any]] = []
    for item in candidate:
        if not isinstance(item, Mapping):
            raise _MalformedDeclaration()
        declarations.append(item)
    try:
        declared_collaboration_protocol = classify_collaboration_request(
            {"tools": declarations, "tool_choice": tool_choice}
        )
    except CollaborationContractError as exc:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            exc.classification,
            surface="request",
        ) from exc
    if collaboration_protocol not in {None, COLLABORATION_V1, COLLABORATION_V2}:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "unknown_state",
            surface="request",
        )
    if (
        declared_collaboration_protocol is not None
        and collaboration_protocol is not None
        and declared_collaboration_protocol != collaboration_protocol
    ):
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "conflicting_selection",
            surface="request",
        )
    effective_collaboration_protocol = (
        declared_collaboration_protocol or collaboration_protocol
    )
    capabilities = _protocol_capabilities(selected_protocol, protocol_capabilities)
    if (
        declared_collaboration_protocol == COLLABORATION_V2
        and selected_protocol != "responses_structured"
        and not (
            capabilities.function_lifecycle
            and capabilities.accepts_namespace_adapter
        )
    ):
        raise RequiredToolUnavailableError(family=NAMESPACE)
    hosted = _provider_hosted(provider_hosted_capabilities)

    all_native_names: set[str] = {str(name) for name in native_names if isinstance(name, str)}
    for declaration in declarations:
        family = classify_declaration(declaration)
        declaration_name = declaration.get("name")
        if isinstance(declaration_name, str) and declaration_name:
            all_native_names.add(declaration_name)
        if family in {PLAIN_FUNCTION, CUSTOM_FREEFORM} and _name_of(declaration):
            all_native_names.add(str(_name_of(declaration)))
        if family == NAMESPACE:
            _namespace, children, _version, _valid = _namespace_details(declaration)
            all_native_names.update(
                str(child.get("name")) for child in children if isinstance(child.get("name"), str)
            )

    registry = RequestScopedToolAliasRegistry(
        request_token=request_token,
        native_names=all_native_names,
        max_tool_name_length=capabilities.max_tool_name_length,
        max_alias_attempts=capabilities.max_alias_attempts,
    )
    preliminary: list[ToolCompatibilityEntry] = []
    for index, declaration in enumerate(declarations):
        family = classify_declaration(declaration)
        is_required = _required_by_rule(
            declaration,
            index,
            required=required,
            tool_choice=tool_choice,
        )
        valid = _declaration_valid_for_family(declaration, family)
        if not valid:
            raise _MalformedDeclaration()
        namespace, children, version, namespace_valid = _namespace_details(declaration)
        if (
            effective_collaboration_protocol == COLLABORATION_V2
            and family == NAMESPACE
            and namespace == "collaboration"
        ):
            is_required = True
        reason = "native_lifecycle"
        disposition = OMIT
        aliases: list[str] = []

        if family == PLAIN_FUNCTION:
            if valid and capabilities.function_lifecycle:
                disposition, reason = NATIVE, "native_function_lifecycle"
            else:
                reason = "function_lifecycle_unavailable"
        elif family == NAMESPACE:
            if valid and capabilities.namespace_lifecycle:
                disposition, reason = NATIVE, "native_namespace_lifecycle"
            elif valid and capabilities.function_lifecycle and capabilities.accepts_namespace_adapter:
                disposition, reason = ADAPT, "namespace_function_adapter"
                for child_index, child in enumerate(children):
                    child_name = str(child.get("name"))
                    aliases.append(
                        registry.allocate_namespace(
                            declaration_index=index,
                            namespace=str(namespace),
                            child_index=child_index,
                            child_name=child_name,
                            version=version,
                        )
                    )
            else:
                reason = "namespace_lifecycle_unavailable"
        elif family == CUSTOM_FREEFORM:
            if valid and capabilities.custom_lifecycle:
                disposition, reason = NATIVE, "native_custom_lifecycle"
            elif valid and capabilities.function_lifecycle and capabilities.accepts_custom_adapter:
                disposition, reason = ADAPT, "custom_function_envelope"
                aliases.append(
                    registry.allocate_custom(
                        declaration_index=index,
                        original_name=str(_name_of(declaration)),
                        version=None,
                    )
                )
            else:
                reason = "custom_lifecycle_unavailable"
        elif family == TOOL_SEARCH:
            if valid and capabilities.tool_search_lifecycle:
                disposition, reason = NATIVE, "native_client_tool_search"
            elif valid and capabilities.function_lifecycle and capabilities.accepts_tool_search_adapter:
                disposition, reason = ADAPT, "tool_search_function_envelope"
                aliases.append(registry.allocate_tool_search(declaration_index=index))
            else:
                reason = "client_tool_search_lifecycle_unavailable"
        elif family == SELECTED_PROVIDER_HOSTED:
            kind = declaration.get("type")
            has_static_contract = _hosted_event_spec_for_declaration_kind(kind) is not None
            if (
                valid
                and isinstance(kind, str)
                and has_static_contract
                and kind in hosted.supported_kinds
                and kind in capabilities.hosted_lifecycles
            ):
                disposition, reason = NATIVE, "selected_provider_hosted_lifecycle"
            else:
                reason = "selected_provider_hosted_lifecycle_unavailable"
        else:
            kind = declaration.get("type")
            provider_hosted = declaration.get("executor") == "selected_provider"
            if provider_hosted and (not isinstance(kind, str) or kind not in hosted.supported_kinds):
                reason = "selected_provider_hosted_lifecycle_unavailable"
            else:
                reason = "unknown_lifecycle_contract_unavailable"

        if disposition == OMIT and is_required:
            disposition = REQUIRED_BUT_UNAVAILABLE
        preliminary.append(
            ToolCompatibilityEntry(
                declaration_index=index,
                family=family,
                disposition=disposition,
                required=is_required,
                declaration=_freeze(_copy_mapping(declaration)),
                reason=reason,
                aliases=tuple(aliases),
                namespace=namespace,
                version=version,
                child_names=tuple(str(child.get("name")) for child in children if isinstance(child.get("name"), str)),
            )
        )

    if _has_explicit_named_tool_choice(tool_choice) and not any(
        _tool_choice_matches_declaration(declaration, tool_choice)
        for declaration in declarations
    ):
        raise RequiredToolUnavailableError()
    if tool_choice in ("required", True) and not any(
        entry.disposition in {NATIVE, ADAPT} for entry in preliminary
    ):
        raise RequiredToolUnavailableError()
    diagnostics = CompatibilityDiagnostics.from_entries(preliminary)
    unavailable = [entry for entry in preliminary if entry.disposition == REQUIRED_BUT_UNAVAILABLE]
    if unavailable:
        raise RequiredToolUnavailableError(family=unavailable[0].family)
    return ToolCompatibilityPlan(
        selected_protocol=str(selected_protocol),
        capabilities=capabilities,
        entries=tuple(preliminary),
        registry=registry,
        diagnostics=diagnostics,
        collaboration_protocol=effective_collaboration_protocol,
        tool_choice=_freeze(tool_choice),
        provider_hosted_kinds=hosted.supported_kinds,
    )

