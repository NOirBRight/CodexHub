"""Declaration families, hosted kinds, and the four compatibility dispositions."""

from __future__ import annotations

from typing import Any, Mapping


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
