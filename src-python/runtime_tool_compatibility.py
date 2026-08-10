"""Request-scoped runtime tool compatibility for the selected Gateway route.

This module deliberately knows only about declaration shape and protocol
capabilities.  It does not select a Provider, execute a tool, or repair a
model response.  The request plan is immutable; the small mutable stream
object is the lifecycle ledger used while assembling one SSE response.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from collaboration_runtime_contract import (
    COLLABORATION_V2,
    CollaborationContractError,
    classify_collaboration_request,
    validate_agent_message,
    validate_collaboration_arguments,
    validate_collaboration_result,
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

CUSTOM_INPUT_KEY = "__codexhub_custom_input"
CUSTOM_OUTPUT_KEY = "__codexhub_custom_output"
TOOL_SEARCH_INPUT_KEY = "__codexhub_tool_search_input"
TOOL_SEARCH_OUTPUT_KEY = "__codexhub_tool_search_output"

_NAMESPACE_ALIAS_PREFIX = "__codexhub_ns_"
_CUSTOM_ALIAS_PREFIX = "__codexhub_custom_"
_TOOL_SEARCH_ALIAS_PREFIX = "__codexhub_search_"
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
_V1_NAMES = frozenset(
    {"spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent"}
)
_V2_NAMES = frozenset(
    {"spawn_agent", "send_message", "followup_task", "wait_agent", "interrupt_agent", "list_agents"}
)
_V1_FORBIDDEN = frozenset({"task_path", "continuation_id", "task_name", "fork_turns"})
_V2_FORBIDDEN = frozenset({"agent_id", "fork_context"})


def _is_opaque_collaboration_history_item(item: Mapping[str, Any]) -> bool:
    if item.get("type") != "function_call":
        return False
    namespace = item.get("namespace")
    name = item.get("name")
    return (
        (namespace == "multi_agent_v1" and name in _V1_NAMES)
        or (namespace == "collaboration" and name in _V2_NAMES)
        or (
            namespace is None
            and isinstance(name, str)
            and name.startswith("multi_agent_v1__")
            and name.removeprefix("multi_agent_v1__") in _V1_NAMES
        )
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw(item) for item in value}
    return value


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return _thaw(_freeze(value))


class ToolCompatibilityError(ValueError):
    """A bounded compatibility failure safe to expose at a wire boundary."""

    def __init__(
        self,
        code: str,
        classification: str,
        *,
        surface: str = "request",
    ) -> None:
        self.code = code
        self.classification = classification
        self.surface = surface
        # Do not include declaration names, aliases, IDs, payloads, or route
        # identity in a compatibility error.  Classification is the only
        # request-derived value that is safe to expose.
        super().__init__(f"Tool compatibility failed at {surface}: {classification}.")


class RequiredToolUnavailableError(ToolCompatibilityError):
    def __init__(self, *, family: str | None = None) -> None:
        # ``family`` is intentionally retained only as a private attribute for
        # tests/telemetry; the public message remains bounded and name-free.
        self.family = family
        super().__init__(
            "tool_compatibility_required_unavailable",
            "required_unavailable",
            surface="request",
        )


class _MalformedDeclaration(ToolCompatibilityError):
    def __init__(self) -> None:
        super().__init__("tool_compatibility_boundary", "malformed_declaration")


@dataclass(frozen=True, slots=True)
class ProtocolCapabilities:
    """Complete lifecycle capabilities of one selected upstream protocol."""

    function_lifecycle: bool = False
    namespace_lifecycle: bool = False
    custom_lifecycle: bool = False
    tool_search_lifecycle: bool = False
    hosted_lifecycles: frozenset[str] = frozenset()
    unknown_lifecycles: frozenset[str] = frozenset()
    accepts_namespace_adapter: bool = False
    accepts_custom_adapter: bool = False
    accepts_tool_search_adapter: bool = False
    max_tool_name_length: int = 128
    max_alias_attempts: int = 128

    def __post_init__(self) -> None:
        for field_name in ("hosted_lifecycles", "unknown_lifecycles"):
            value = getattr(self, field_name)
            if not isinstance(value, frozenset):
                object.__setattr__(self, field_name, frozenset(str(item) for item in value))
        if self.max_tool_name_length <= 0 or self.max_alias_attempts <= 0:
            raise ValueError("protocol compatibility limits must be positive")

    @classmethod
    def chat_tools(cls, **overrides: Any) -> "ProtocolCapabilities":
        values: dict[str, Any] = {
            "function_lifecycle": True,
            "namespace_lifecycle": False,
            "custom_lifecycle": False,
            "tool_search_lifecycle": False,
            "accepts_namespace_adapter": True,
            "accepts_custom_adapter": True,
            "accepts_tool_search_adapter": True,
        }
        values.update(overrides)
        return cls(**values)

    @classmethod
    def responses_structured(cls, **overrides: Any) -> "ProtocolCapabilities":
        values: dict[str, Any] = {
            "function_lifecycle": True,
            "namespace_lifecycle": True,
            "custom_lifecycle": True,
            "tool_search_lifecycle": True,
        }
        values.update(overrides)
        return cls(**values)

    @classmethod
    def for_protocol(
        cls,
        protocol: str,
        facts: Mapping[str, Any] | None = None,
    ) -> "ProtocolCapabilities":
        facts = facts or {}
        normalized = str(protocol).strip().lower()
        if normalized in {"chat", "chat_tools", "chat_completions"}:
            defaults = cls.chat_tools()
        elif normalized in {"responses", "responses_structured"}:
            # The protocol name alone proves only the plain-function wire
            # lifecycle. Native namespace/custom/search support requires
            # explicit lifecycle facts; adapters remain request-scoped.
            defaults = cls.chat_tools()
        else:
            defaults = cls()

        def boolean(*keys: str, default: bool) -> bool:
            for key in keys:
                if key in facts and isinstance(facts[key], bool):
                    return bool(facts[key])
            return default

        hosted = facts.get("hosted_lifecycles", facts.get("hosted_kinds", ()))
        if isinstance(hosted, Mapping):
            hosted = [key for key, value in hosted.items() if value is True]
        if isinstance(hosted, str):
            hosted = [hosted]
        unknown = facts.get("unknown_lifecycles", facts.get("unknown_kinds", ()))
        if isinstance(unknown, Mapping):
            unknown = [key for key, value in unknown.items() if value is True]
        if isinstance(unknown, str):
            unknown = [unknown]

        return cls(
            function_lifecycle=boolean("function_lifecycle", "supports_functions", default=defaults.function_lifecycle),
            namespace_lifecycle=boolean("namespace_lifecycle", "supports_namespace", "supports_namespaces", default=defaults.namespace_lifecycle),
            custom_lifecycle=boolean("custom_lifecycle", "supports_custom", "supports_custom_tools", default=defaults.custom_lifecycle),
            tool_search_lifecycle=boolean("tool_search_lifecycle", "supports_tool_search", default=defaults.tool_search_lifecycle),
            hosted_lifecycles=frozenset(str(item) for item in hosted),
            unknown_lifecycles=frozenset(str(item) for item in unknown),
            accepts_namespace_adapter=boolean(
                "accepts_namespace_adapter", "namespace_adapter", default=defaults.accepts_namespace_adapter
            ),
            accepts_custom_adapter=boolean(
                "accepts_custom_adapter", "custom_adapter", default=defaults.accepts_custom_adapter
            ),
            accepts_tool_search_adapter=boolean(
                "accepts_tool_search_adapter", "tool_search_adapter", default=defaults.accepts_tool_search_adapter
            ),
            max_tool_name_length=int(facts.get("max_tool_name_length", defaults.max_tool_name_length)),
            max_alias_attempts=int(facts.get("max_alias_attempts", defaults.max_alias_attempts)),
        )


@dataclass(frozen=True, slots=True)
class HostedCapabilityFacts:
    supported_kinds: frozenset[str] = frozenset()

    @classmethod
    def from_value(cls, value: Any) -> "HostedCapabilityFacts":
        if isinstance(value, HostedCapabilityFacts):
            return value
        if isinstance(value, Mapping):
            return cls(frozenset(str(key) for key, enabled in value.items() if enabled is True))
        if isinstance(value, str):
            return cls(frozenset({value}))
        if isinstance(value, Iterable):
            return cls(frozenset(str(item) for item in value))
        return cls()


@dataclass(frozen=True, slots=True)
class CompatibilityDiagnostics:
    counts: tuple[tuple[str, str, int], ...] = ()
    failures: tuple[str, ...] = ()

    @classmethod
    def from_entries(cls, entries: Iterable["ToolCompatibilityEntry"]) -> "CompatibilityDiagnostics":
        counts = Counter((entry.family, entry.disposition) for entry in entries)
        return cls(
            counts=tuple(
                (family, disposition, count)
                for (family, disposition), count in sorted(counts.items())
            )
        )

    def __repr__(self) -> str:
        return f"CompatibilityDiagnostics(counts={self.counts!r}, failures={self.failures!r})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": [
                {"family": family, "disposition": disposition, "count": count}
                for family, disposition, count in self.counts
            ],
            "failure_classifications": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class AliasRecord:
    alias: str
    family: str
    declaration_index: int
    child_index: int | None
    namespace: str | None
    child_name: str | None
    original_name: str | None
    version: str | None


class RequestScopedToolAliasRegistry:
    """One request-local, opaque alias map with explicit reverse lookups."""

    def __init__(
        self,
        *,
        request_token: str,
        native_names: Iterable[str] = (),
        max_tool_name_length: int = 128,
        max_alias_attempts: int = 128,
    ) -> None:
        self._token = hashlib.sha256(str(request_token).encode("utf-8")).hexdigest()[:10]
        self._native_names = frozenset(str(name) for name in native_names if isinstance(name, str))
        self._max_length = max_tool_name_length
        self._max_attempts = max_alias_attempts
        self._aliases: dict[str, AliasRecord] = {}
        self._by_declaration: dict[tuple[int, int | None], str] = {}
        self._calls: dict[str, AliasRecord] = {}
        # ``max_alias_attempts`` bounds collision probing for one allocation;
        # it must not cap the total number of aliases in a request.  Keep the
        # next ordinal per alias family so a request with more than that many
        # adapted namespace/custom tools remains representable.
        self._next_ordinals: dict[str, int] = {}

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._aliases)

    def reserve_native_names(self, names: Iterable[str]) -> dict[str, str]:
        requested = {str(name) for name in names if isinstance(name, str) and name}
        self._native_names = frozenset(
            set(self._native_names)
            | requested
        )
        remapped: dict[str, str] = {}
        for alias, record in tuple(self._aliases.items()):
            if alias not in requested:
                continue
            self._aliases.pop(alias, None)
            self._by_declaration.pop((record.declaration_index, record.child_index), None)
            prefix = {
                NAMESPACE: _NAMESPACE_ALIAS_PREFIX,
                CUSTOM_FREEFORM: _CUSTOM_ALIAS_PREFIX,
                TOOL_SEARCH: _TOOL_SEARCH_ALIAS_PREFIX,
            }.get(record.family, _CUSTOM_ALIAS_PREFIX)
            replacement = self._allocate(record, prefix)
            remapped[alias] = replacement
        return remapped

    def is_native_name(self, value: Any) -> bool:
        return isinstance(value, str) and value in self._native_names

    @staticmethod
    def looks_like_alias(value: Any) -> bool:
        return isinstance(value, str) and value.startswith(
            (_NAMESPACE_ALIAS_PREFIX, _CUSTOM_ALIAS_PREFIX, _TOOL_SEARCH_ALIAS_PREFIX)
        )

    def _allocate(self, record_without_alias: AliasRecord, prefix: str) -> str:
        start_ordinal = self._next_ordinals.get(prefix, 1)
        for offset in range(self._max_attempts):
            ordinal = start_ordinal + offset
            candidate = f"{prefix}{self._token}_{ordinal}"
            if len(candidate) > self._max_length:
                raise ToolCompatibilityError(
                    "tool_compatibility_alias_limit",
                    "alias_length_exhausted",
                )
            if candidate in self._native_names or candidate in self._aliases:
                continue
            record = AliasRecord(
                alias=candidate,
                family=record_without_alias.family,
                declaration_index=record_without_alias.declaration_index,
                child_index=record_without_alias.child_index,
                namespace=record_without_alias.namespace,
                child_name=record_without_alias.child_name,
                original_name=record_without_alias.original_name,
                version=record_without_alias.version,
            )
            self._aliases[candidate] = record
            self._by_declaration[(record.declaration_index, record.child_index)] = candidate
            self._next_ordinals[prefix] = ordinal + 1
            return candidate
        raise ToolCompatibilityError(
            "tool_compatibility_alias_limit",
            "alias_collision_exhausted",
        )

    def allocate_namespace(
        self,
        *,
        declaration_index: int,
        namespace: str,
        child_index: int,
        child_name: str,
        version: str | None,
    ) -> str:
        return self._allocate(
            AliasRecord(
                alias="",
                family=NAMESPACE,
                declaration_index=declaration_index,
                child_index=child_index,
                namespace=namespace,
                child_name=child_name,
                original_name=child_name,
                version=version,
            ),
            _NAMESPACE_ALIAS_PREFIX,
        )

    def allocate_custom(self, *, declaration_index: int, original_name: str, version: str | None) -> str:
        return self._allocate(
            AliasRecord(
                alias="",
                family=CUSTOM_FREEFORM,
                declaration_index=declaration_index,
                child_index=None,
                namespace=None,
                child_name=None,
                original_name=original_name,
                version=version,
            ),
            _CUSTOM_ALIAS_PREFIX,
        )

    def allocate_tool_search(self, *, declaration_index: int) -> str:
        return self._allocate(
            AliasRecord(
                alias="",
                family=TOOL_SEARCH,
                declaration_index=declaration_index,
                child_index=None,
                namespace=None,
                child_name="tool_search",
                original_name="tool_search",
                version=None,
            ),
            _TOOL_SEARCH_ALIAS_PREFIX,
        )

    def record_for_alias(self, alias: Any) -> AliasRecord | None:
        return self._aliases.get(alias) if isinstance(alias, str) else None

    def alias_for(self, declaration_index: int, child_index: int | None = None) -> str | None:
        return self._by_declaration.get((declaration_index, child_index))

    def bind_call(self, call_id: Any, alias: str) -> None:
        if not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity")
        record = self.record_for_alias(alias)
        if record is None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias")
        previous = self._calls.get(call_id)
        if previous is not None and previous.alias != alias:
            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity")
        if previous is not None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity")
        self._calls[call_id] = record

    def record_for_call(self, call_id: Any) -> AliasRecord | None:
        return self._calls.get(call_id) if isinstance(call_id, str) else None

    def new_attempt(self) -> "RequestScopedToolAliasRegistry":
        """Copy immutable alias ownership while starting a fresh call ledger.

        Alias allocation and declaration ownership are request-scoped and must
        remain stable across permitted upstream retries.  Call ownership is
        stream-attempt scoped, however: a provider may legitimately reuse a
        call id after a transport failure, so a retry cannot inherit ``_calls``
        from the failed attempt.
        """
        attempt = object.__new__(RequestScopedToolAliasRegistry)
        attempt._token = self._token
        attempt._native_names = self._native_names
        attempt._max_length = self._max_length
        attempt._max_attempts = self._max_attempts
        attempt._aliases = dict(self._aliases)
        attempt._by_declaration = dict(self._by_declaration)
        attempt._calls = {}
        attempt._next_ordinals = dict(self._next_ordinals)
        return attempt


@dataclass(frozen=True, slots=True)
class ToolCompatibilityEntry:
    declaration_index: int
    family: str
    disposition: str
    required: bool
    declaration: Mapping[str, Any]
    reason: str
    aliases: tuple[str, ...] = ()
    namespace: str | None = None
    version: str | None = None
    child_names: tuple[str, ...] = ()

    @property
    def original_name(self) -> str | None:
        name = self.declaration.get("name")
        return name if isinstance(name, str) else None


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


def _declaration_key(declaration: Mapping[str, Any]) -> tuple[Any, ...]:
    family = classify_declaration(declaration)
    # ``tool_search`` has two equivalent declaration spellings in the
    # Responses/Chat boundary: the native client-owned item and the explicit
    # function-shaped fallback used by older providers.  Treat those
    # spellings as one request declaration so finalization cannot inject a
    # second search entry with a different alias.
    if family == TOOL_SEARCH:
        return (family, "tool_search")
    return (
        family,
        declaration.get("type"),
        declaration.get("name"),
        declaration.get("namespace"),
    )


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


def _protocol_capabilities(
    selected_protocol: str,
    supplied: ProtocolCapabilities | Mapping[str, Any] | None,
) -> ProtocolCapabilities:
    if isinstance(supplied, ProtocolCapabilities):
        return supplied
    return ProtocolCapabilities.for_protocol(selected_protocol, supplied)


def _provider_hosted(value: Any) -> HostedCapabilityFacts:
    return HostedCapabilityFacts.from_value(value)


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
) -> "ToolCompatibilityPlan":
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
        collaboration_version = classify_collaboration_request(
            {"tools": declarations, "tool_choice": tool_choice}
        )
    except CollaborationContractError as exc:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            exc.classification,
            surface="request",
        ) from exc
    if (
        collaboration_version == COLLABORATION_V2
        and selected_protocol != "responses_structured"
    ):
        raise RequiredToolUnavailableError(family=NAMESPACE)
    capabilities = _protocol_capabilities(selected_protocol, protocol_capabilities)
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
            collaboration_version == COLLABORATION_V2
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
        tool_choice=_freeze(tool_choice),
        provider_hosted_kinds=hosted.supported_kinds,
    )


def _json_object_exact(value: Any, *, output: bool = False) -> dict[str, Any]:
    if isinstance(value, Mapping):
        parsed = _copy_mapping(value)
    elif isinstance(value, str):
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in items:
                if key in result:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_envelope_key")
                result[key] = item
            return result

        try:
            parsed = json.loads(value, object_pairs_hook=pairs)
        except ToolCompatibilityError:
            raise
        except (TypeError, ValueError):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope") from None
    else:
        raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
    expected = CUSTOM_OUTPUT_KEY if output else CUSTOM_INPUT_KEY
    if not isinstance(parsed, dict) or set(parsed) != {expected}:
        raise ToolCompatibilityError("tool_compatibility_boundary", "invalid_envelope")
    return parsed


def _json_object_with_key(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        parsed = _copy_mapping(value)
    elif isinstance(value, str):
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for item_key, item in items:
                if item_key in result:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_envelope_key")
                result[item_key] = item
            return result

        try:
            parsed = json.loads(value, object_pairs_hook=pairs)
        except ToolCompatibilityError:
            raise
        except (TypeError, ValueError):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope") from None
    else:
        raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
    if not isinstance(parsed, dict) or set(parsed) != {key}:
        raise ToolCompatibilityError("tool_compatibility_boundary", "invalid_envelope")
    return parsed


def _dump_envelope(key: str, value: Any) -> str:
    return json.dumps({key: _thaw(_freeze(value))}, ensure_ascii=True, separators=(",", ":"))


def _validate_version_fields(item: Mapping[str, Any], record: AliasRecord) -> None:
    if record.version == "v1" and _V1_FORBIDDEN.intersection(item):
        raise ToolCompatibilityError("tool_compatibility_boundary", "mixed_v1_v2_fields")
    if record.version == "v2" and _V2_FORBIDDEN.intersection(item):
        raise ToolCompatibilityError("tool_compatibility_boundary", "mixed_v1_v2_fields")
    arguments = item.get("arguments")
    if arguments in (None, ""):
        return
    if isinstance(arguments, Mapping):
        parsed = _copy_mapping(arguments)
    elif isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_arguments") from None
    else:
        raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_arguments")
    if not isinstance(parsed, Mapping):
        raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_arguments")
    if record.version == "v1" and _V1_FORBIDDEN.intersection(parsed):
        raise ToolCompatibilityError("tool_compatibility_boundary", "mixed_v1_v2_fields")
    if record.version == "v2" and _V2_FORBIDDEN.intersection(parsed):
        raise ToolCompatibilityError("tool_compatibility_boundary", "mixed_v1_v2_fields")


def _item_identity(item: Mapping[str, Any]) -> str | None:
    item_id = item.get("item_id")
    plain_id = item.get("id")
    if (
        isinstance(item_id, str)
        and item_id
        and isinstance(plain_id, str)
        and plain_id
        and item_id != plain_id
    ):
        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity")
    for key in ("item_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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


@dataclass(frozen=True, slots=True)
class ToolCompatibilityPlan:
    selected_protocol: str
    capabilities: ProtocolCapabilities
    entries: tuple[ToolCompatibilityEntry, ...]
    registry: RequestScopedToolAliasRegistry = field(repr=False, compare=False)
    diagnostics: CompatibilityDiagnostics
    tool_choice: Any = None
    provider_hosted_kinds: frozenset[str] = frozenset()

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.registry.aliases

    @property
    def has_adaptations(self) -> bool:
        return any(entry.disposition == ADAPT for entry in self.entries)

    def entry(self, index: int) -> ToolCompatibilityEntry:
        return self.entries[index]

    def new_stream(self) -> "CompatibilityStreamState":
        return CompatibilityStreamState(self)

    def new_attempt(self) -> "ToolCompatibilityPlan":
        """Return an attempt-local plan with stable aliases and fresh calls."""
        return ToolCompatibilityPlan(
            selected_protocol=self.selected_protocol,
            capabilities=self.capabilities,
            entries=self.entries,
            registry=self.registry.new_attempt(),
            diagnostics=self.diagnostics,
            tool_choice=self.tool_choice,
            provider_hosted_kinds=self.provider_hosted_kinds,
        )

    def _collaboration_v2_entry(self) -> ToolCompatibilityEntry | None:
        matches = [
            entry
            for entry in self.entries
            if entry.family == NAMESPACE
            and entry.version == "v2"
            and entry.namespace == "collaboration"
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _raise_collaboration_contract(
        error: CollaborationContractError,
        *,
        surface: str,
    ) -> None:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            error.classification,
            surface=surface,
        ) from error

    def _validate_collaboration_v2_items(
        self,
        items: Any,
        *,
        surface: str,
    ) -> None:
        if self._collaboration_v2_entry() is None or not isinstance(items, list):
            return
        calls: dict[str, str] = {}
        seen_item_ids: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "agent_message":
                try:
                    validate_agent_message(item)
                except CollaborationContractError as exc:
                    self._raise_collaboration_contract(exc, surface=surface)
                item_id = item.get("id")
            elif (
                item_type == "function_call"
                and item.get("namespace") == "collaboration"
                and item.get("name") in _V2_NAMES
            ):
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_item_identity",
                        surface=surface,
                    )
                allowed_fields = {
                    "type",
                    "id",
                    "call_id",
                    "namespace",
                    "name",
                    "arguments",
                }
                if surface in {"response", "stream"}:
                    allowed_fields.add("status")
                valid_field_sets = [allowed_fields]
                if "status" in allowed_fields:
                    valid_field_sets.append(allowed_fields - {"status"})
                if set(item) not in valid_field_sets:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "collaboration_call_fields_invalid",
                        surface=surface,
                    )
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_call_identity",
                        surface=surface,
                    )
                if call_id in calls:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_call_identity",
                        surface=surface,
                    )
                name = str(item["name"])
                try:
                    validate_collaboration_arguments(
                        COLLABORATION_V2,
                        name,
                        item.get("arguments"),
                    )
                except CollaborationContractError as exc:
                    self._raise_collaboration_contract(exc, surface=surface)
                calls[call_id] = name
            elif item_type == "function_call_output" and item.get("call_id") in calls:
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_item_identity",
                        surface=surface,
                    )
                if set(item) != {"type", "id", "call_id", "output"}:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "collaboration_result_fields_invalid",
                        surface=surface,
                    )
                try:
                    validate_collaboration_result(
                        COLLABORATION_V2,
                        calls[str(item["call_id"])],
                        item.get("output"),
                    )
                except CollaborationContractError as exc:
                    self._raise_collaboration_contract(exc, surface=surface)
            else:
                continue
            if not isinstance(item_id, str) or not item_id:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "missing_item_identity",
                    surface=surface,
                )
            if item_id in seen_item_ids:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "duplicate_item_identity",
                    surface=surface,
                )
            seen_item_ids.add(item_id)

    def with_final_declarations(
        self,
        declarations: Iterable[Mapping[str, Any]],
        *,
        tool_choice: Any = None,
    ) -> "ToolCompatibilityPlan":
        """Add model-visible native declarations while preserving request aliases."""
        final = list(declarations)
        entries = list(self.entries)
        effective_tool_choice = self.tool_choice if tool_choice is None else tool_choice
        native_names: set[str] = set()
        for declaration in final:
            if not isinstance(declaration, Mapping):
                continue
            name = declaration.get("name")
            if isinstance(name, str):
                native_names.add(name)
            family = classify_declaration(declaration)
            if family == NAMESPACE:
                _namespace, children, _version, _valid = _namespace_details(declaration)
                native_names.update(
                    str(child.get("name")) for child in children if isinstance(child.get("name"), str)
                )
            key = _declaration_key(declaration)
            if any(_declaration_key(entry.declaration) == key for entry in self.entries):
                continue
            if not _declaration_valid_for_family(declaration, family):
                continue
            is_required = _required_by_rule(
                declaration,
                len(entries),
                required=None,
                tool_choice=effective_tool_choice,
            )
            disposition = OMIT
            reason = "lifecycle_unavailable"
            aliases: list[str] = []
            if family == PLAIN_FUNCTION:
                if self.capabilities.function_lifecycle:
                    disposition, reason = NATIVE, "native_function_lifecycle"
            elif family == NAMESPACE:
                namespace, children, version, _valid = _namespace_details(declaration)
                if self.capabilities.namespace_lifecycle:
                    disposition, reason = NATIVE, "native_namespace_lifecycle"
                elif self.capabilities.function_lifecycle and self.capabilities.accepts_namespace_adapter:
                    disposition, reason = ADAPT, "namespace_function_adapter"
                    aliases = [
                        self.registry.allocate_namespace(
                            declaration_index=len(entries),
                            namespace=str(namespace),
                            child_index=child_index,
                            child_name=str(child.get("name")),
                            version=version,
                        )
                        for child_index, child in enumerate(children)
                    ]
            elif family == CUSTOM_FREEFORM:
                if self.capabilities.custom_lifecycle:
                    disposition, reason = NATIVE, "native_custom_lifecycle"
                elif self.capabilities.function_lifecycle and self.capabilities.accepts_custom_adapter:
                    disposition, reason = ADAPT, "custom_function_envelope"
                    aliases = [
                        self.registry.allocate_custom(
                            declaration_index=len(entries),
                            original_name=str(_name_of(declaration)),
                            version=None,
                        )
                    ]
            elif family == TOOL_SEARCH:
                if self.capabilities.tool_search_lifecycle:
                    disposition, reason = NATIVE, "native_client_tool_search"
                elif self.capabilities.function_lifecycle and self.capabilities.accepts_tool_search_adapter:
                    disposition, reason = ADAPT, "tool_search_function_envelope"
                    aliases = [self.registry.allocate_tool_search(declaration_index=len(entries))]
            elif family == SELECTED_PROVIDER_HOSTED:
                # Provider capability facts are intentionally not inferred for
                # declarations added after the request plan was built.
                if (
                    _hosted_event_spec_for_declaration_kind(declaration.get("type")) is not None
                    and declaration.get("type") in self.provider_hosted_kinds
                    and declaration.get("type") in self.capabilities.hosted_lifecycles
                ):
                    disposition, reason = NATIVE, "selected_provider_hosted_lifecycle"
            elif family == UNKNOWN_FUTURE_KIND:
                reason = "unknown_lifecycle_contract_unavailable"
            if disposition == OMIT and is_required:
                disposition = REQUIRED_BUT_UNAVAILABLE
                reason = "required_unavailable"
            entries.append(
                ToolCompatibilityEntry(
                    declaration_index=len(entries),
                    family=family,
                    disposition=disposition,
                    required=is_required,
                    declaration=_freeze(_copy_mapping(declaration)),
                    reason=reason,
                    aliases=tuple(aliases),
                    namespace=_namespace_details(declaration)[0] if family == NAMESPACE else None,
                    version=_namespace_details(declaration)[2] if family == NAMESPACE else None,
                    child_names=tuple(
                        str(child.get("name"))
                        for child in _namespace_details(declaration)[1]
                        if isinstance(child.get("name"), str)
                    ) if family == NAMESPACE else (),
                )
            )
        # ``_set_required_subagent_tool_choice`` may already have translated a
        # namespace child to this request's generated alias.  That alias is a
        # valid choice even though it is not the original declaration spelling
        # present in ``final``.  Unknown aliases remain fail-closed.
        choice_name = (
            effective_tool_choice
            if isinstance(effective_tool_choice, str)
            else effective_tool_choice.get("name")
            if isinstance(effective_tool_choice, Mapping)
            else None
        )
        choice_is_known_alias = self.registry.record_for_alias(choice_name) is not None
        if _has_explicit_named_tool_choice(effective_tool_choice) and not choice_is_known_alias and not any(
            _tool_choice_matches_declaration(declaration, effective_tool_choice)
            for declaration in final
            if isinstance(declaration, Mapping)
        ):
            raise RequiredToolUnavailableError()
        if any(entry.disposition == REQUIRED_BUT_UNAVAILABLE for entry in entries):
            raise RequiredToolUnavailableError()
        alias_remap = self.registry.reserve_native_names(native_names)
        if alias_remap:
            entries = [
                ToolCompatibilityEntry(
                    declaration_index=entry.declaration_index,
                    family=entry.family,
                    disposition=entry.disposition,
                    required=entry.required,
                    declaration=entry.declaration,
                    reason=entry.reason,
                    aliases=tuple(alias_remap.get(alias, alias) for alias in entry.aliases),
                    namespace=entry.namespace,
                    version=entry.version,
                    child_names=entry.child_names,
                )
                for entry in entries
            ]
        if len(entries) == len(self.entries) and not alias_remap:
            return self
        return ToolCompatibilityPlan(
            selected_protocol=self.selected_protocol,
            capabilities=self.capabilities,
            entries=tuple(entries),
            registry=self.registry,
            diagnostics=CompatibilityDiagnostics.from_entries(entries),
            tool_choice=self.tool_choice,
            provider_hosted_kinds=self.provider_hosted_kinds,
        )

    def _entry_for_declaration(self, declaration: Mapping[str, Any], occurrence: dict[tuple[Any, ...], int]) -> ToolCompatibilityEntry | None:
        key = _declaration_key(declaration)
        matching = [entry for entry in self.entries if _declaration_key(entry.declaration) == key]
        offset = occurrence.get(key, 0)
        occurrence[key] = offset + 1
        return matching[offset] if offset < len(matching) else None

    @staticmethod
    def _family_for_item_type(item_type: Any, namespace: Any = None) -> str | None:
        if item_type == "function_call":
            return NAMESPACE if namespace is not None else PLAIN_FUNCTION
        if item_type == "custom_tool_call":
            return CUSTOM_FREEFORM
        if item_type in {"tool_search_call", "tool_search_output"}:
            return TOOL_SEARCH
        if isinstance(item_type, str) and item_type.endswith("_call"):
            kind = item_type[: -len("_call")]
            if kind in _KNOWN_HOSTED_TYPES:
                return SELECTED_PROVIDER_HOSTED
        return None

    def _entry_for_name(
        self,
        name: Any,
        namespace: Any = None,
        *,
        expected_family: str | None = None,
        item_type: Any = None,
    ) -> ToolCompatibilityEntry | None:
        if not isinstance(name, str):
            return None
        expected_family = expected_family or self._family_for_item_type(item_type, namespace)
        matches: list[ToolCompatibilityEntry] = []
        if namespace is not None:
            matches = [
                entry
                for entry in self.entries
                if entry.family == NAMESPACE
                and entry.namespace == namespace
                and name in entry.child_names
            ]
        else:
            # An unqualified child name is not enough to identify an adapted
            # namespace call.  Prefer an exact non-namespace declaration (in
            # particular a native plain function) and only accept the
            # explicit flattened ``namespace__child`` spelling for an
            # adapted namespace call.
            matches = [entry for entry in self.entries if entry.family != NAMESPACE and entry.original_name == name]
            if not matches:
                matches = [
                    entry
                    for entry in self.entries
                    if entry.family == NAMESPACE
                    and any(name == f"{entry.namespace}__{child}" for child in entry.child_names)
                ]
        if expected_family is not None:
            filtered = [entry for entry in matches if entry.family == expected_family]
            if (
                not filtered
                and expected_family == PLAIN_FUNCTION
                and namespace is None
                and any(entry.family == NAMESPACE for entry in matches)
            ):
                expected_family = NAMESPACE
                filtered = [entry for entry in matches if entry.family == NAMESPACE]
            matches = filtered
        if len(matches) > 1 and (expected_family is not None or item_type is not None):
            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity")
        return matches[0] if len(matches) == 1 else None

    def _entries_for_name(
        self,
        name: Any,
        namespace: Any = None,
        *,
        expected_family: str | None = None,
        item_type: Any = None,
    ) -> list[ToolCompatibilityEntry]:
        if not isinstance(name, str):
            return []
        expected_family = expected_family or self._family_for_item_type(item_type, namespace)
        matches: list[ToolCompatibilityEntry] = []
        for entry in self.entries:
            if entry.family == NAMESPACE:
                if namespace is not None and namespace != entry.namespace:
                    continue
                if namespace is not None and name in entry.child_names:
                    matches.append(entry)
                elif namespace is None and any(
                    name == f"{entry.namespace}__{child}" for child in entry.child_names
                ):
                    matches.append(entry)
            elif entry.original_name == name:
                if namespace is None:
                    matches.append(entry)
        if expected_family is not None:
            filtered = [entry for entry in matches if entry.family == expected_family]
            if (
                not filtered
                and expected_family == PLAIN_FUNCTION
                and namespace is None
                and any(entry.family == NAMESPACE for entry in matches)
            ):
                expected_family = NAMESPACE
                filtered = [entry for entry in matches if entry.family == NAMESPACE]
            matches = filtered
        if len(matches) > 1 and (expected_family is not None or item_type is not None):
            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity")
        return matches

    def _has_unqualified_adapted_child(self, name: Any) -> bool:
        return isinstance(name, str) and any(
            entry.family == NAMESPACE
            and entry.disposition == ADAPT
            and name in entry.child_names
            for entry in self.entries
        )

    def _has_adapted_name_conflict(self, name: Any, expected_family: str) -> bool:
        if not isinstance(name, str):
            return False
        if any(
            entry.disposition == NATIVE
            and entry.family == expected_family
            and entry.original_name == name
            for entry in self.entries
        ):
            return False
        return any(
            entry.disposition == ADAPT
            and entry.family != expected_family
            and entry.original_name == name
            for entry in self.entries
        )

    def _alias_for(self, entry: ToolCompatibilityEntry, *, child_name: str | None = None) -> str | None:
        if entry.family == NAMESPACE:
            if child_name not in entry.child_names and isinstance(child_name, str):
                child_name = next(
                    (
                        child
                        for child in entry.child_names
                        if child_name == f"{entry.namespace}__{child}"
                    ),
                    child_name,
                )
            try:
                child_index = entry.child_names.index(child_name or "")
            except ValueError:
                return None
            return entry.aliases[child_index]
        return entry.aliases[0] if entry.aliases else None

    def owns_wire_value(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        if _is_opaque_collaboration_history_item(value):
            return True
        name = value.get("name")
        namespace = value.get("namespace")
        item_type = value.get("type")
        if self.registry.record_for_alias(name) is not None:
            return True
        if self.registry.looks_like_alias(name):
            return True
        if self._entry_for_name(
            name,
            namespace,
            expected_family=self._family_for_item_type(item_type, namespace),
            item_type=item_type,
        ) is not None:
            return True
        return self.registry.record_for_call(value.get("call_id")) is not None

    def _encode_tool_declarations(self, tools: Any) -> tuple[Any, bool]:
        if not isinstance(tools, list):
            return tools, False
        occurrence: dict[tuple[Any, ...], int] = {}
        encoded: list[Any] = []
        changed = False
        for raw_tool in tools:
            if not isinstance(raw_tool, Mapping):
                encoded.append(raw_tool)
                continue
            entry = self._entry_for_declaration(raw_tool, occurrence)
            if entry is None:
                encoded.append(_copy_mapping(raw_tool))
                continue
            if entry.disposition == OMIT:
                changed = True
                continue
            if entry.disposition != ADAPT:
                encoded.append(_copy_mapping(raw_tool))
                continue
            if entry.family == NAMESPACE:
                namespace, children, _version, valid = _namespace_details(raw_tool)
                if not valid:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_declaration")
                for child_index, child in enumerate(children):
                    function = _copy_mapping(child)
                    function["type"] = "function"
                    function["name"] = entry.aliases[child_index]
                    function.pop("namespace", None)
                    encoded.append(function)
                changed = True
                continue
            if entry.family == CUSTOM_FREEFORM:
                alias = entry.aliases[0]
                encoded.append(
                    {
                        "type": "function",
                        "name": alias,
                        "parameters": {
                            "type": "object",
                            "properties": {CUSTOM_INPUT_KEY: {}},
                            "required": [CUSTOM_INPUT_KEY],
                            "additionalProperties": False,
                        },
                    }
                )
                changed = True
                continue
            if entry.family == TOOL_SEARCH:
                alias = entry.aliases[0]
                function = _copy_mapping(raw_tool)
                function["type"] = "function"
                function["name"] = alias
                function.pop("execution", None)
                function.pop("description", None)
                source_parameters = function.get("parameters")
                if not isinstance(source_parameters, Mapping):
                    source_parameters = {"type": "object"}
                function["parameters"] = {
                    "type": "object",
                    "properties": {TOOL_SEARCH_INPUT_KEY: source_parameters},
                    "required": [TOOL_SEARCH_INPUT_KEY],
                    "additionalProperties": False,
                }
                encoded.append(function)
                changed = True
                continue
            encoded.append(_copy_mapping(raw_tool))
        return encoded, changed

    def _encode_tool_choice(self, value: Any) -> tuple[Any, bool]:
        thawed = _thaw(value)
        if isinstance(thawed, str):
            entry = self._entry_for_name(thawed)
            if entry is None and thawed == "tool_search":
                candidates = [
                    candidate
                    for candidate in self.entries
                    if candidate.family == TOOL_SEARCH
                ]
                if len(candidates) == 1:
                    entry = candidates[0]
            alias = self._alias_for(entry) if entry is not None and entry.disposition == ADAPT else None
            return (alias, True) if alias else (thawed, False)
        if not isinstance(thawed, Mapping):
            return thawed, False
        result = dict(thawed)
        name = result.get("name")
        choice_type = result.get("type")
        namespace_choice = name if isinstance(name, str) else result.get("namespace")
        if choice_type == "namespace" and isinstance(namespace_choice, str):
            candidates = [
                entry
                for entry in self.entries
                if entry.family == NAMESPACE
                and entry.namespace == namespace_choice
                and entry.disposition == ADAPT
            ]
            if candidates:
                aliases = [alias for entry in candidates for alias in entry.aliases]
                if len(aliases) != 1:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_tool_choice")
                result["type"] = "function"
                result["name"] = aliases[0]
                return result, True
        entry = self._entry_for_name(name, result.get("namespace"))
        if choice_type == "function" and isinstance(name, str):
            candidates = [
                candidate
                for candidate in self._entries_for_name(name, result.get("namespace"))
                if candidate.disposition == ADAPT
            ]
            if len(candidates) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_tool_choice")
        alias = (
            self._alias_for(entry, child_name=name)
            if entry is not None and entry.disposition == ADAPT
            else None
        )
        if alias is None and name == "tool_search" and result.get("type") == "function":
            candidates = [
                candidate
                for candidate in self.entries
                if candidate.family == TOOL_SEARCH and candidate.disposition == ADAPT
            ]
            if len(candidates) == 1:
                alias = candidates[0].aliases[0]
        if alias:
            result["name"] = alias
            result["type"] = "function"
            result.pop("namespace", None)
            return result, True
        return result, False

    def _encode_item(self, raw_item: Any, call_aliases: dict[str, str]) -> tuple[Any, bool]:
        if not isinstance(raw_item, Mapping):
            return raw_item, False
        item = _copy_mapping(raw_item)
        item_type = item.get("type")
        name = item.get("name")
        namespace = item.get("namespace")
        if item_type in {"tool_search_call", "tool_search_output"}:
            native_entry = self._native_entry_for_item(item)
            if native_entry is not None:
                self._validate_native_item(item, native_entry)
        if _hosted_output_kind_for_item_type(item_type) is not None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="history")
        entry = self._entry_for_name(name, namespace, item_type=item_type)
        if entry is None and item_type in {"tool_search_call", "tool_search_output"}:
            candidates = [candidate for candidate in self.entries if candidate.family == TOOL_SEARCH]
            if len(candidates) == 1:
                entry = candidates[0]
        if item_type == "function_call" and entry is not None and entry.disposition == ADAPT:
            alias = self._alias_for(entry, child_name=name)
            if alias is None:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias")
            _validate_version_fields(item, self.registry.record_for_alias(alias))  # type: ignore[arg-type]
            call_id = item.get("call_id")
            self.registry.bind_call(call_id, alias)
            call_aliases[str(call_id)] = alias
            item["name"] = alias
            item.pop("namespace", None)
            if entry.family == CUSTOM_FREEFORM:
                if "input" not in item:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
                item["type"] = "function_call"
                item["arguments"] = _dump_envelope(CUSTOM_INPUT_KEY, item.pop("input"))
            return item, True
        if item_type == "custom_tool_call" and entry is not None and entry.disposition == ADAPT:
            alias = self._alias_for(entry)
            if alias is None or "input" not in item:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
            call_id = item.get("call_id")
            self.registry.bind_call(call_id, alias)
            call_aliases[str(call_id)] = alias
            item["type"] = "function_call"
            item["name"] = alias
            item["arguments"] = _dump_envelope(CUSTOM_INPUT_KEY, item.pop("input"))
            return item, True
        if item_type == "tool_search_call" and entry is not None and entry.disposition == ADAPT:
            alias = self._alias_for(entry)
            if alias is None:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias")
            if item.get("execution") != "client":
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "invalid_tool_search_execution",
                    surface="history",
                )
            call_id = item.get("call_id")
            self.registry.bind_call(call_id, alias)
            call_aliases[str(call_id)] = alias
            item["type"] = "function_call"
            item["name"] = alias
            item.pop("namespace", None)
            item.pop("execution", None)
            item["arguments"] = _dump_envelope(TOOL_SEARCH_INPUT_KEY, item.pop("arguments", {}))
            return item, True
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            alias = call_aliases.get(call_id) if isinstance(call_id, str) else None
            record = self.registry.record_for_call(call_id) if alias is None else self.registry.record_for_alias(alias)
            if record is not None and record.family == CUSTOM_FREEFORM:
                if "output" not in item:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope")
                item["type"] = "function_call_output"
                item["output"] = _dump_envelope(CUSTOM_OUTPUT_KEY, item["output"])
                return item, True
            if record is not None and record.family == NAMESPACE:
                _validate_version_fields(item, record)
            return item, False
        if item_type == "tool_search_output":
            call_id = item.get("call_id")
            alias = call_aliases.get(call_id) if isinstance(call_id, str) else None
            record = self.registry.record_for_call(call_id) if alias is None else self.registry.record_for_alias(alias)
            if record is not None and record.family == TOOL_SEARCH:
                if item.get("execution") != "client":
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "invalid_tool_search_execution",
                        surface="history",
                    )
                payload = _copy_mapping(item)
                for key in ("type", "execution", "id", "item_id", "call_id"):
                    payload.pop(key, None)
                item["type"] = "function_call_output"
                item["output"] = _dump_envelope(TOOL_SEARCH_OUTPUT_KEY, payload)
                for key in payload:
                    item.pop(key, None)
                item.pop("execution", None)
                return item, True
            return item, False
        return item, False

    @staticmethod
    def _hosted_entry_item_kind(entry: ToolCompatibilityEntry) -> str | None:
        declaration_type = entry.declaration.get("type")
        if entry.family == SELECTED_PROVIDER_HOSTED:
            declaration_spec = _hosted_event_spec_for_declaration_kind(declaration_type)
            if declaration_spec is not None:
                return declaration_spec[0]
            return declaration_type if isinstance(declaration_type, str) else None
        if (
            entry.family == UNKNOWN_FUTURE_KIND
            and entry.declaration.get("executor") == "selected_provider"
            and isinstance(declaration_type, str)
        ):
            return declaration_type
        return None

    def _hosted_history_item_key(self, item_type: Any) -> tuple[str, bool] | None:
        # Standard Responses tool lifecycles are resolved by their declaration
        # family (and, for adapted calls, the request-scoped alias registry).
        # Never reinterpret them as a provider-hosted ``<kind>_call`` merely
        # because an unrelated unknown declaration happens to use the same
        # prefix (for example ``custom_tool``).  Keeping this guard at the
        # plan-level entry point is important for request-history filtering as
        # well as response decoding.
        if item_type in {
            "function_call",
            "function_call_output",
            "custom_tool_call",
            "custom_tool_call_output",
            "tool_search_call",
            "tool_search_output",
        }:
            return None
        key = _hosted_history_item_key(item_type)
        if key is not None or not isinstance(item_type, str):
            return key
        for entry in self.entries:
            if entry.disposition not in {NATIVE, OMIT}:
                continue
            hosted_kind = self._hosted_entry_item_kind(entry)
            if hosted_kind is None:
                continue
            if item_type == f"{hosted_kind}_call":
                return hosted_kind, False
            if item_type == f"{hosted_kind}_call_output":
                return hosted_kind, True
        return None

    @staticmethod
    def _unknown_response_item_kind(item_type: Any) -> str | None:
        if not isinstance(item_type, str):
            return None
        if item_type.endswith("_call_output"):
            kind = item_type[: -len("_call_output")]
            return kind if kind else None
        if item_type.endswith("_call"):
            kind = item_type[: -len("_call")]
            return kind if kind else None
        return None

    def _unknown_entry_for_item(
        self,
        item: Mapping[str, Any],
        *,
        surface: str,
    ) -> ToolCompatibilityEntry | None:
        unknown_kind = self._unknown_response_item_kind(item.get("type"))
        if unknown_kind is None:
            return None
        matches = [
            entry
            for entry in self.entries
            if entry.family == UNKNOWN_FUTURE_KIND
            and entry.disposition in {NATIVE, OMIT}
            and entry.declaration.get("type") == unknown_kind
        ]
        item_name = item.get("name")
        if isinstance(item_name, str):
            named_matches = [
                entry for entry in matches if entry.declaration.get("name") == item_name
            ]
            if named_matches:
                matches = named_matches
            elif any(isinstance(entry.declaration.get("name"), str) for entry in matches):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface=surface,
                )
        if len(matches) > 1:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface=surface,
            )
        return matches[0] if matches else None

    @staticmethod
    def _history_output_type_for_entry(entry: ToolCompatibilityEntry) -> str | None:
        if entry.family in {PLAIN_FUNCTION, NAMESPACE}:
            return "function_call_output"
        if entry.family == CUSTOM_FREEFORM:
            return "custom_tool_call_output"
        if entry.family == TOOL_SEARCH:
            return "tool_search_output"
        if entry.family == UNKNOWN_FUTURE_KIND:
            declaration_type = entry.declaration.get("type")
            if isinstance(declaration_type, str):
                return f"{declaration_type}_call_output"
        return None

    def _expected_response_output_type(self, entry: ToolCompatibilityEntry) -> str | None:
        expected = self._history_output_type_for_entry(entry)
        if entry.disposition == ADAPT and entry.family in {NAMESPACE, CUSTOM_FREEFORM, TOOL_SEARCH}:
            return "function_call_output"
        return expected

    def _standard_contract_families_for_item(self, item: Mapping[str, Any]) -> frozenset[str]:
        item_type = item.get("type")
        if item_type == "function_call":
            return frozenset({NAMESPACE}) if item.get("namespace") is not None else frozenset({PLAIN_FUNCTION, NAMESPACE})
        if item_type == "custom_tool_call":
            return frozenset({CUSTOM_FREEFORM})
        if item_type in {"tool_search_call", "tool_search_output"}:
            return frozenset({TOOL_SEARCH})
        return frozenset()

    def _has_standard_contract_for_item(self, item: Mapping[str, Any]) -> bool:
        families = self._standard_contract_families_for_item(item)
        # Only an omitted standard declaration creates a representational
        # boundary that an unknown item could bypass.  Native-only plans may
        # still carry provider-local historical calls that are intentionally
        # left untouched.
        return bool(families) and any(
            entry.family in families and entry.disposition == OMIT
            for entry in self.entries
        )

    def _reject_unknown_standard_item(self, item: Mapping[str, Any], *, surface: str) -> None:
        """Reject an undeclared standard lifecycle when this plan has a contract.

        A standard item with a typo or a provider-local name must not bypass an
        omitted/native declaration merely because name lookup returned no exact
        match.  Adapter aliases remain valid function-call wire names.
        """
        if surface == "history" and _is_opaque_collaboration_history_item(item):
            return
        if item.get("type") == "function_call" and self.registry.record_for_alias(item.get("name")) is not None:
            return
        if (
            item.get("type") == "custom_tool_call"
            and item.get("namespace") is not None
            and any(
                entry.family == CUSTOM_FREEFORM and entry.disposition == ADAPT
                for entry in self.entries
            )
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface=surface,
            )
        if (
            item.get("type") == "function_call"
            and item.get("namespace") is None
            and isinstance(item.get("name"), str)
        ):
            name = item.get("name")
            has_native_plain_owner = any(
                entry.family == PLAIN_FUNCTION
                and entry.disposition == NATIVE
                and entry.original_name == name
                for entry in self.entries
            )
            has_native_namespace_owner = any(
                entry.family == NAMESPACE
                and entry.disposition == NATIVE
                and name in entry.child_names
                for entry in self.entries
            )
            if has_native_namespace_owner and not has_native_plain_owner:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface=surface,
                )
        if (
            item.get("type") == "function_call"
            and item.get("namespace") is not None
            and isinstance(item.get("name"), str)
        ):
            flattened_name = f"{item.get('namespace')}__{item.get('name')}"
            flattened_matches = [
                entry
                for entry in self.entries
                if entry.family == PLAIN_FUNCTION
                and entry.original_name == flattened_name
            ]
            if any(entry.disposition == NATIVE for entry in flattened_matches):
                return
            if flattened_matches:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface=surface,
                )
            if any(entry.family == NAMESPACE for entry in self.entries) and self._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item.get("type"),
            ) is None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface=surface,
                )
        if not self._has_standard_contract_for_item(item):
            return
        if self._standard_entry_for_item(item, surface=surface) is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface=surface,
            )

    def _entry_for_call_owner(
        self,
        item: Mapping[str, Any],
        *,
        surface: str,
    ) -> ToolCompatibilityEntry | None:
        """Resolve a call item to its request/response declaration owner."""
        if item.get("type") not in {"function_call", "custom_tool_call", "tool_search_call"}:
            return None
        entry = self._native_entry_for_item(item)
        if entry is None:
            record = self.registry.record_for_alias(item.get("name"))
            if record is not None:
                entry = next(
                    (
                        candidate
                        for candidate in self.entries
                        if candidate.declaration_index == record.declaration_index
                    ),
                    None,
                )
        if entry is None:
            entry = self._standard_entry_for_item(item, surface=surface)
        if entry is None:
            # Older Responses providers emit the client-owned search as a
            # plain ``function_call`` named ``tool_search`` even when the
            # request declaration was adapted to an opaque alias.  Resolve
            # that exact legacy spelling to the one adapted search entry;
            # unrelated provider functions with the same name are not
            # eligible because ``_legacy_tool_search_record`` only considers
            # an explicit adapted search declaration.
            legacy_record = self._legacy_tool_search_record(item)
            if legacy_record is not None:
                entry = next(
                    (
                        candidate
                        for candidate in self.entries
                        if candidate.declaration_index == legacy_record.declaration_index
                    ),
                    None,
                )
        if entry is None:
            entry = self._unknown_entry_for_item(item, surface=surface)
        return entry

    def _standard_entry_for_item(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None = None,
        surface: str = "response",
    ) -> ToolCompatibilityEntry | None:
        """Resolve a standard Responses item before hosted/unknown matching.

        A provider-specific declaration such as ``custom_tool`` shares the
        ``custom_tool_call`` prefix with the standard custom lifecycle.  The
        standard declaration (or request-scoped adapted call identity) must
        therefore win before the generic hosted matcher is consulted.  For
        output items the wire shape has no tool name, so prefer a native
        standard family when one exists and otherwise retain an omitted
        standard family as the fail-closed result.
        """
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            return self._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item_type,
            )

        if item_type == "tool_search_call":
            matches = [
                entry
                for entry in self.entries
                if entry.family == TOOL_SEARCH and entry.disposition in {NATIVE, ADAPT, OMIT}
            ]
            native = next((entry for entry in matches if entry.disposition == NATIVE), None)
            return native or (matches[0] if matches else None)

        if item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
            if owner is not None:
                expected_output_type = (
                    self._expected_response_output_type(owner)
                    if surface == "response"
                    else self._history_output_type_for_entry(owner)
                )
                if item_type != expected_output_type:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface=surface,
                    )
                return owner
            record = self.registry.record_for_call(item.get("call_id"))
            if record is not None:
                entry = next(
                    (
                        entry
                        for entry in self.entries
                        if entry.declaration_index == record.declaration_index
                    ),
                    None,
                )
                # Registry records describe adapter wire calls.  Both the
                # namespace and custom adapters emit a function lifecycle;
                # a differently typed output cannot borrow that call_id.
                expected_output_type = (
                    "function_call_output"
                    if record.family in {NAMESPACE, CUSTOM_FREEFORM}
                    else None
                )
                if item_type != expected_output_type:
                    if self._unknown_entry_for_item(item, surface=surface) is not None:
                        return None
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface=surface,
                    )
                return entry
            if item_type == "function_call_output":
                families = {PLAIN_FUNCTION, NAMESPACE}
            elif item_type == "custom_tool_call_output":
                families = {CUSTOM_FREEFORM}
            else:
                families = {TOOL_SEARCH}
            matches = [
                entry
                for entry in self.entries
                if entry.family in families and entry.disposition in {NATIVE, OMIT}
            ]
            if matches and self._unknown_entry_for_item(item, surface=surface) is not None:
                # Without a call owner, the output spelling alone cannot
                # distinguish a standard lifecycle from an exact unknown kind
                # that uses the same prefix.  Let the unknown OMIT matcher
                # reject it instead of silently claiming it as native.
                return None
            native = next((entry for entry in matches if entry.disposition == NATIVE), None)
            return native or (matches[0] if matches else None)
        return None

    def _validate_response_owner_item(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None,
        call_index: int | None,
        item_index: int,
        surface: str,
    ) -> None:
        """Validate output-family ownership before response decoding starts."""
        item_type = item.get("type")
        if not isinstance(item_type, str) or (
            item_type != "tool_search_output" and not item_type.endswith("_call_output")
        ):
            return
        if owner is None:
            return
        expected = (
            self._history_output_type_for_entry(owner)
            if surface == "history"
            else self._expected_response_output_type(owner)
        )
        if expected is not None and item_type != expected:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_call_identity",
                surface=surface,
            )
        if surface == "response" and call_index is not None and item_index < call_index:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_call_identity",
                surface=surface,
            )

    def _omitted_response_entry_for_item(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None = None,
    ) -> ToolCompatibilityEntry | None:
        standard_entry = self._standard_entry_for_item(item, owner=owner, surface="response")
        if standard_entry is not None:
            return standard_entry if standard_entry.disposition == OMIT else None
        hosted_item_key = self._hosted_history_item_key(item.get("type"))
        if hosted_item_key is not None:
            hosted_item_kind, _is_output = hosted_item_key
            matches = [
                entry
                for entry in self.entries
                if entry.disposition in {NATIVE, OMIT}
                and self._hosted_entry_item_kind(entry) == hosted_item_kind
            ]
            if len(matches) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="response")
            return matches[0] if matches and matches[0].disposition == OMIT else None
        unknown_entry = self._unknown_entry_for_item(item, surface="response")
        return unknown_entry if unknown_entry is not None and unknown_entry.disposition == OMIT else None

    def _omitted_history_entry(
        self,
        item: Mapping[str, Any],
        *,
        owner: ToolCompatibilityEntry | None = None,
    ) -> ToolCompatibilityEntry | None:
        item_type = item.get("type")
        standard_entry = self._standard_entry_for_item(item, owner=owner, surface="history")
        if standard_entry is not None:
            return standard_entry if standard_entry.disposition == OMIT else None
        hosted_item_key = self._hosted_history_item_key(item_type)
        if hosted_item_key is not None:
            hosted_item_kind, _is_output = hosted_item_key
            matches = [
                entry
                for entry in self.entries
                if entry.disposition in {NATIVE, OMIT}
                and self._hosted_entry_item_kind(entry) == hosted_item_kind
            ]
            if len(matches) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="history")
            if matches and matches[0].disposition == OMIT:
                return matches[0]
            return None
        unknown_entry = self._unknown_entry_for_item(item, surface="history")
        if unknown_entry is not None:
            return unknown_entry if unknown_entry.disposition == OMIT else None
        if item_type in {"function_call", "custom_tool_call"}:
            entry = self._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item_type,
            )
            if entry is not None and entry.disposition == OMIT:
                return entry
            self._reject_unknown_standard_item(item, surface="history")
            return None
        self._reject_unknown_standard_item(item, surface="history")
        if item_type in {"tool_search_call", "tool_search_output"}:
            return next(
                (
                    entry
                    for entry in self.entries
                    if entry.family == TOOL_SEARCH and entry.disposition == OMIT
                ),
                None,
            )
        if isinstance(item_type, str):
            return next(
                (
                    entry
                    for entry in self.entries
                    if entry.disposition == OMIT
                    and isinstance(entry.declaration.get("type"), str)
                    and item_type.startswith(f"{entry.declaration.get('type')}_")
                ),
                None,
            )
        return None

    def encode_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = _copy_mapping(payload)
        encoded_tools, tools_changed = self._encode_tool_declarations(result.get("tools"))
        if tools_changed:
            result["tools"] = encoded_tools
        chosen, choice_changed = self._encode_tool_choice(result.get("tool_choice"))
        if choice_changed:
            result["tool_choice"] = chosen
        call_aliases: dict[str, str] = {}
        raw_input = result.get("input")
        if isinstance(raw_input, list):
            self._validate_collaboration_v2_items(raw_input, surface="history")
            encoded_input: list[Any] = []
            changed = tools_changed or choice_changed
            history_call_owners: dict[str, ToolCompatibilityEntry] = {}
            history_call_positions: dict[str, int] = {}
            seen_history_call_ids: dict[str, ToolCompatibilityEntry | None] = {}
            for item_index, item in enumerate(raw_input):
                if not isinstance(item, Mapping) or item.get("type") not in {
                    "function_call",
                    "custom_tool_call",
                    "tool_search_call",
                }:
                    continue
                owner = self._entry_for_call_owner(item, surface="history")
                call_id = item.get("call_id")
                if isinstance(call_id, str) and call_id:
                    if call_id in seen_history_call_ids:
                        previous = seen_history_call_ids[call_id]
                        classification = (
                            "ambiguous_call_identity"
                            if previous is not None
                            and owner is not None
                            and (previous.disposition == OMIT or owner.disposition == OMIT)
                            else "duplicate_call_identity"
                        )
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            classification,
                            surface="history",
                        )
                    seen_history_call_ids[call_id] = owner
                    if owner is not None:
                        history_call_owners[call_id] = owner
                        history_call_positions[call_id] = item_index

            def omitted_history_entry(item: Mapping[str, Any]) -> ToolCompatibilityEntry | None:
                owner = None
                if item.get("type") in {
                    "function_call_output",
                    "custom_tool_call_output",
                    "tool_search_output",
                }:
                    call_id = item.get("call_id")
                    if isinstance(call_id, str):
                        owner = history_call_owners.get(call_id)
                return self._omitted_history_entry(item, owner=owner)

            def validate_history_output_owner(item: Mapping[str, Any], item_index: int) -> None:
                item_type = item.get("type")
                is_result = isinstance(item_type, str) and (
                    item_type == "tool_search_output" or item_type.endswith("_call_output")
                )
                if not is_result:
                    return
                omitted_entry = omitted_history_entry(item)
                if omitted_entry is not None:
                    return
                call_id = item.get("call_id")
                owner = history_call_owners.get(call_id) if isinstance(call_id, str) else None
                if owner is None:
                    # A standard output can be retained when the call lives
                    # outside this request's history slice.  Unknown output
                    # families have no such safe interpretation: without a
                    # request-local owner they must fail closed.
                    if item_type in {
                        "function_call_output",
                        "custom_tool_call_output",
                        "tool_search_output",
                    }:
                        return
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_call_identity",
                        surface="history",
                    )
                self._validate_response_owner_item(
                    item,
                    owner=owner,
                    call_index=history_call_positions.get(call_id),
                    item_index=item_index,
                    surface="history",
                )

            seen_history_item_ids: set[str] = set()
            seen_history_output_call_ids: set[str] = set()
            for item_index, item in enumerate(raw_input):
                if not isinstance(item, Mapping):
                    continue
                item_type = item.get("type")
                validate_history_output_owner(item, item_index)
                omitted_entry = omitted_history_entry(item)
                hosted_item_key = self._hosted_history_item_key(item_type)
                is_optional_omitted_hosted = omitted_entry is not None and hosted_item_key is not None
                if not is_optional_omitted_hosted:
                    item_id = _item_identity(item)
                    if item_id is not None:
                        if item_id in seen_history_item_ids:
                            raise ToolCompatibilityError(
                                "tool_compatibility_boundary",
                                "duplicate_item_identity",
                                surface="history",
                            )
                        seen_history_item_ids.add(item_id)
                if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                    call_id = item.get("call_id")
                    if omitted_entry is None and (not isinstance(call_id, str) or not call_id):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "missing_call_identity",
                            surface="history",
                        )
                if isinstance(item_type, str) and (
                    item_type == "tool_search_output" or item_type.endswith("_call_output")
                ):
                    call_id = item.get("call_id")
                    if (
                        not is_optional_omitted_hosted
                        and isinstance(call_id, str)
                        and call_id
                    ):
                        if call_id in seen_history_output_call_ids:
                            raise ToolCompatibilityError(
                                "tool_compatibility_boundary",
                                "duplicate_call_identity",
                                surface="history",
                            )
                        seen_history_output_call_ids.add(call_id)

            omitted_hosted_ids: dict[str, tuple[str, bool, bool]] = {}
            for item in raw_input:
                if not isinstance(item, Mapping):
                    continue
                omitted_entry = omitted_history_entry(item)
                hosted_item_key = self._hosted_history_item_key(item.get("type"))
                if (
                    omitted_entry is None
                    or hosted_item_key is None
                ):
                    continue
                item_id = _item_identity(item)
                if item_id is None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_item_identity",
                        surface="history",
                    )
                hosted_item_kind, is_output = hosted_item_key
                previous = omitted_hosted_ids.get(item_id)
                if previous is not None:
                    previous_kind, previous_is_output, pair_consumed = previous
                    if previous_kind != hosted_item_kind:
                        classification = "ambiguous_native_identity"
                    elif pair_consumed or previous_is_output == is_output:
                        classification = "duplicate_item_identity"
                    else:
                        # Some hosted protocols use one item id for call/result
                        # pairs; others use distinct ids.  Both are valid when
                        # the hosted kind is already uniquely omitted.
                        omitted_hosted_ids[item_id] = (hosted_item_kind, is_output, True)
                        continue
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        classification,
                        surface="history",
                    )
                omitted_hosted_ids[item_id] = (hosted_item_kind, is_output, False)
            omitted_call_items = [
                item
                for item in raw_input
                if isinstance(item, Mapping)
                and omitted_history_entry(item) is not None
                and not (
                    self._hosted_history_item_key(item.get("type")) is not None
                    if omitted_history_entry(item) is not None
                    else False
                )
                and (
                    item.get("type") in {"function_call", "custom_tool_call", "tool_search_call"}
                    or (isinstance(item.get("type"), str) and item.get("type").endswith("_call"))
                )
            ]
            omitted_call_ids: set[str] = set()
            for item in omitted_call_items:
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
                if call_id in omitted_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="history")
                omitted_call_ids.add(call_id)
            retained_call_ids = {
                item.get("call_id")
                for item in raw_input
                if isinstance(item, Mapping)
                and (
                    item.get("type") in {"function_call", "custom_tool_call", "tool_search_call"}
                    or (isinstance(item.get("type"), str) and item.get("type").endswith("_call"))
                )
                and omitted_history_entry(item) is None
                and isinstance(item.get("call_id"), str)
            }
            if omitted_call_ids.intersection(retained_call_ids):
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="history")
            for item in raw_input:
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
                    call_id = item.get("call_id")
                    if not isinstance(call_id, str) or not call_id:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
                    if omitted_history_entry(item) is not None and call_id not in omitted_call_ids:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="history")
            for item in raw_input:
                if isinstance(item, Mapping) and (
                    (
                        omitted_history_entry(item) is not None
                        and self._hosted_history_item_key(item.get("type")) is None
                    )
                    or item.get("call_id") in omitted_call_ids
                ):
                    changed = True
                    continue
                if isinstance(item, Mapping):
                    omitted_entry = omitted_history_entry(item)
                    hosted_item_key = self._hosted_history_item_key(item.get("type"))
                    if (
                        omitted_entry is not None
                        and hosted_item_key is not None
                    ):
                        changed = True
                        continue
                encoded, item_changed = self._encode_item(item, call_aliases)
                encoded_input.append(encoded)
                changed = changed or item_changed
            if changed:
                result["input"] = encoded_input
        return result

    def _decode_call(self, item: Mapping[str, Any], *, allow_incomplete: bool = False) -> tuple[dict[str, Any], AliasRecord | None, bool]:
        result = _copy_mapping(item)
        name = result.get("name")
        record = self.registry.record_for_alias(name)
        if record is None:
            if self.registry.looks_like_alias(name):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
            return result, None, False
        _validate_version_fields(result, record)
        if record.family == NAMESPACE and result.get("namespace") not in {None, record.namespace}:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
        if record.family == CUSTOM_FREEFORM and result.get("namespace") is not None:
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
        call_id = result.get("call_id")
        if isinstance(call_id, str) and call_id:
            previous = self.registry.record_for_call(call_id)
            if previous is not None and previous.alias != record.alias:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="response",
                )
            if previous is None:
                self.registry.bind_call(call_id, record.alias)
        elif not allow_incomplete:
            raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="response")
        result["name"] = record.child_name if record.family == NAMESPACE else record.original_name
        if record.family == NAMESPACE:
            result["namespace"] = record.namespace
        elif record.family == CUSTOM_FREEFORM:
            if "arguments" in result:
                envelope = _json_object_exact(result["arguments"])
                result["type"] = "custom_tool_call"
                result["input"] = envelope[CUSTOM_INPUT_KEY]
                result.pop("arguments", None)
            elif not allow_incomplete:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope", surface="response")
            else:
                result["type"] = "custom_tool_call"
        elif record.family == TOOL_SEARCH:
            result.pop("name", None)
            result.pop("namespace", None)
            result["type"] = "tool_search_call"
            result["execution"] = "client"
            if "arguments" in result and result.get("arguments") is not None and result.get("arguments") != "":
                envelope = _json_object_with_key(result["arguments"], TOOL_SEARCH_INPUT_KEY)
                result["arguments"] = envelope[TOOL_SEARCH_INPUT_KEY]
            elif not allow_incomplete:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope", surface="response")
        return result, record, True

    def _legacy_tool_search_record(self, item: Mapping[str, Any]) -> AliasRecord | None:
        """Return the one adapted search record for legacy function-shaped SSE.

        Older third-party Responses routes emit the original ``tool_search``
        function name rather than the request-scoped alias.  Accept that shape
        only when this request has exactly one explicit client search entry;
        an unrelated provider function with the same name remains plain.
        """

        if item.get("type") != "function_call" or item.get("name") != "tool_search":
            return None
        # A request may legitimately contain a provider-owned ordinary
        # function with the same spelling as Codex's client search.  Once
        # that declaration exists, the unqualified legacy wire item is
        # ambiguous and must remain provider-owned instead of being silently
        # rebound to the adapted search alias.
        if any(
            entry.family == PLAIN_FUNCTION and entry.original_name == "tool_search"
            for entry in self.entries
        ):
            return None
        candidates = [
            entry
            for entry in self.entries
            if entry.family == TOOL_SEARCH
            and entry.original_name == "tool_search"
            and entry.disposition == ADAPT
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _decode_call_compat(
        self,
        item: Mapping[str, Any],
        *,
        allow_incomplete: bool = False,
    ) -> tuple[dict[str, Any], AliasRecord | None, bool]:
        legacy_record = self._legacy_tool_search_record(item)
        if legacy_record is None:
            return self._decode_call(item, allow_incomplete=allow_incomplete)
        transformed = _copy_mapping(item)
        transformed["name"] = legacy_record.aliases[0]
        arguments = transformed.get("arguments")
        if arguments is not None and arguments != "":
            if isinstance(arguments, str):
                try:
                    arguments_value = json.loads(arguments)
                except (TypeError, ValueError) as exc:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "malformed_envelope",
                        surface="response",
                    ) from exc
            elif isinstance(arguments, Mapping):
                arguments_value = arguments
            else:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "malformed_envelope",
                    surface="response",
                )
            transformed["arguments"] = json.dumps(
                {TOOL_SEARCH_INPUT_KEY: arguments_value},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        return self._decode_call(transformed, allow_incomplete=allow_incomplete)

    def _validate_registered_item_identity(self, item: Mapping[str, Any], *, surface: str) -> None:
        """Ensure a bound adapter call keeps its wire family and alias."""
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        record = self.registry.record_for_call(call_id)
        if record is None:
            return
        item_type = item.get("type")
        if item_type == "function_call":
            if item.get("name") != record.alias:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface=surface,
                )
            return
        if item_type == "function_call_output":
            if record.family == CUSTOM_FREEFORM and item.get("namespace") is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_alias",
                    surface=surface,
                )
            return
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "ambiguous_call_identity",
            surface=surface,
        )

    def _decode_result(self, item: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        result = _copy_mapping(item)
        call_id = result.get("call_id")
        record = self.registry.record_for_call(call_id)
        if record is None:
            if self.registry.looks_like_alias(result.get("name")):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="response")
            return result, False
        _validate_version_fields(result, record)
        if record.family == CUSTOM_FREEFORM:
            if "output" not in result:
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_envelope", surface="response")
            envelope = _json_object_exact(result["output"], output=True)
            result["type"] = "custom_tool_call_output"
            result["output"] = envelope[CUSTOM_OUTPUT_KEY]
        elif record.family == TOOL_SEARCH:
            envelope = _json_object_with_key(result.get("output"), TOOL_SEARCH_OUTPUT_KEY)
            payload = envelope[TOOL_SEARCH_OUTPUT_KEY]
            if not isinstance(payload, Mapping):
                raise ToolCompatibilityError("tool_compatibility_boundary", "invalid_envelope", surface="response")
            result["type"] = "tool_search_output"
            result["execution"] = "client"
            result.pop("output", None)
            for key, value in payload.items():
                if key in {"type", "execution", "id", "item_id", "call_id"}:
                    continue
                result[key] = value
        return result, True

    def _native_entry_for_item(self, item: Mapping[str, Any]) -> ToolCompatibilityEntry | None:
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            entry = self._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item_type,
            )
            if entry is not None and entry.disposition == NATIVE:
                return entry
            # A native plain declaration can be surfaced by providers using
            # the explicit namespace/child shape that normally belongs to a
            # native namespace declaration.  Resolve only the exact,
            # injective ``namespace__child`` spelling; arbitrary namespace
            # decoration must remain an unknown identity.
            if item_type == "function_call":
                namespace = item.get("namespace")
                child_name = item.get("name")
                if isinstance(namespace, str) and namespace and isinstance(child_name, str) and child_name:
                    flattened_name = f"{namespace}__{child_name}"
                    flattened_matches = [
                        candidate
                        for candidate in self.entries
                        if candidate.family == PLAIN_FUNCTION
                        and candidate.disposition == NATIVE
                        and candidate.original_name == flattened_name
                    ]
                    if len(flattened_matches) > 1:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                        )
                    if flattened_matches:
                        return flattened_matches[0]
            return None
        if item_type in {"tool_search_call", "tool_search_output"}:
            matches = [
                entry
                for entry in self.entries
                if entry.family == TOOL_SEARCH and entry.disposition == NATIVE
            ]
            return matches[0] if len(matches) == 1 else None
        hosted_item_spec = _hosted_event_spec_for_item_type(item_type)
        if hosted_item_spec is not None and item_type == hosted_item_spec[0]:
            hosted_event_kind, _stages = hosted_item_spec
            matches = [
                entry
                for entry in self.entries
                if entry.family == SELECTED_PROVIDER_HOSTED
                and entry.disposition == NATIVE
                and (
                    declaration_spec := _hosted_event_spec_for_declaration_kind(entry.declaration.get("type"))
                ) is not None
                and declaration_spec[0] == hosted_event_kind
            ]
            if len(matches) > 1:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity")
            return matches[0] if matches else None
        return None

    def _has_native_structural_family(self) -> bool:
        return any(
            entry.disposition == NATIVE
            and entry.family in {PLAIN_FUNCTION, NAMESPACE, CUSTOM_FREEFORM, TOOL_SEARCH}
            for entry in self.entries
        )

    @staticmethod
    def _validate_native_item(
        item: Mapping[str, Any],
        entry: ToolCompatibilityEntry,
        *,
        require_completed: bool = False,
        surface: str = "history",
    ) -> None:
        if entry.version is not None:
            _validate_version_fields(
                item,
                AliasRecord(
                    alias="",
                    family=entry.family,
                    declaration_index=entry.declaration_index,
                    child_index=None,
                    namespace=entry.namespace,
                    child_name=item.get("name") if isinstance(item.get("name"), str) else None,
                    original_name=entry.original_name,
                    version=entry.version,
                ),
            )
        if (
            entry.version == "v2"
            and entry.family == NAMESPACE
            and item.get("arguments") is not None
            and item.get("arguments") != ""
        ):
            try:
                validate_collaboration_arguments(
                    COLLABORATION_V2,
                    str(item.get("name")),
                    item.get("arguments"),
                )
            except CollaborationContractError as exc:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    exc.classification,
                    surface=surface,
                ) from exc
        item_type = item.get("type")
        if entry.family == PLAIN_FUNCTION:
            namespace = item.get("namespace")
            plain_shape = namespace is None and item.get("name") == entry.original_name
            flattened_shape = (
                isinstance(namespace, str)
                and namespace
                and isinstance(item.get("name"), str)
                and item.get("name")
                and f"{namespace}__{item.get('name')}" == entry.original_name
            )
            if item_type != "function_call" or not (plain_shape or flattened_shape):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
        elif entry.family == NAMESPACE:
            if (
                item_type != "function_call"
                or item.get("namespace") != entry.namespace
                or item.get("name") not in entry.child_names
            ):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
        elif entry.family == CUSTOM_FREEFORM:
            if item_type != "custom_tool_call" or item.get("name") != entry.original_name:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
        elif entry.family == TOOL_SEARCH:
            if item_type not in {"tool_search_call", "tool_search_output"}:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
            if item.get("execution") != "client":
                raise ToolCompatibilityError("tool_compatibility_boundary", "invalid_tool_search_execution", surface=surface)
        elif entry.family == SELECTED_PROVIDER_HOSTED:
            hosted_spec = _hosted_event_spec_for_declaration_kind(entry.declaration.get("type"))
            if hosted_spec is None or item_type != hosted_spec[0]:
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_native_identity", surface=surface)
            if _item_identity(item) is None:
                raise ToolCompatibilityError("tool_compatibility_boundary", "missing_item_identity", surface=surface)
            if require_completed and item.get("status") not in {None, "completed"}:
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_hosted_lifecycle", surface=surface)

    def _decode_items(self, items: Any, *, reject_omitted_response: bool = False) -> tuple[Any, bool]:
        if not isinstance(items, list):
            return items, False
        result: list[Any] = []
        changed = False
        seen_item_ids: set[str] = set()
        seen_call_ids: set[str] = set()
        seen_result_call_ids: set[str] = set()
        response_call_owners: dict[str, ToolCompatibilityEntry] = {}
        response_call_positions: dict[str, int] = {}
        surface = "response" if reject_omitted_response else "history"
        for item_index, raw_item in enumerate(items):
            if not isinstance(raw_item, Mapping):
                continue
            if raw_item.get("type") not in {"function_call", "custom_tool_call", "tool_search_call"}:
                continue
            call_id = raw_item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            owner = self._entry_for_call_owner(raw_item, surface=surface)
            if owner is None:
                continue
            previous = response_call_owners.get(call_id)
            if previous is not None:
                classification = (
                    "ambiguous_call_identity"
                    if previous.disposition == OMIT or owner.disposition == OMIT
                    else "duplicate_call_identity"
                )
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    classification,
                    surface=surface,
                )
            response_call_owners[call_id] = owner
            response_call_positions[call_id] = item_index
        response_output_call_ids: set[str] = set()
        for item_index, raw_item in enumerate(items):
            if not isinstance(raw_item, Mapping):
                continue
            call_id = raw_item.get("call_id")
            item_type = raw_item.get("type")
            if isinstance(item_type, str) and (
                item_type == "tool_search_output" or item_type.endswith("_call_output")
            ):
                if isinstance(call_id, str) and call_id:
                    if call_id in response_output_call_ids:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "duplicate_call_identity",
                            surface=surface,
                        )
                    response_output_call_ids.add(call_id)
                owner = response_call_owners.get(call_id) if isinstance(call_id, str) else None
                self._validate_response_owner_item(
                    raw_item,
                    owner=owner,
                    call_index=response_call_positions.get(call_id) if isinstance(call_id, str) else None,
                    item_index=item_index,
                    surface=surface,
                )
        for raw_item in items:
            if not isinstance(raw_item, Mapping):
                result.append(raw_item)
                continue
            item_type = raw_item.get("type")
            item = _copy_mapping(raw_item)
            call_id = item.get("call_id")
            owner = response_call_owners.get(call_id) if isinstance(call_id, str) else None
            self._validate_registered_item_identity(item, surface=surface)
            if reject_omitted_response and self._omitted_response_entry_for_item(item, owner=owner) is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface=surface,
                )
            item_id = _item_identity(item)
            if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
            if isinstance(item_id, str) and item_id:
                if item_id in seen_item_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="history")
                seen_item_ids.add(item_id)
            if isinstance(call_id, str) and call_id:
                if call_id in seen_call_ids and item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="history")
                if item_type in {"function_call", "custom_tool_call", "tool_search_call"}:
                    seen_call_ids.add(call_id)
            if item_type == "function_call":
                record = self.registry.record_for_alias(item.get("name"))
                legacy_record = self._legacy_tool_search_record(item)
                if record is not None or legacy_record is not None:
                    decoded, _record, item_changed = self._decode_call_compat(item)
                else:
                    native_entry = self._native_entry_for_item(item)
                    if native_entry is not None:
                        self._validate_native_item(item, native_entry)
                        decoded, item_changed = item, False
                    elif self.registry.looks_like_alias(item.get("name")):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                    elif self._has_adapted_name_conflict(item.get("name"), PLAIN_FUNCTION):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                    elif self.has_adaptations and (
                        self._entry_for_name(
                            item.get("name"),
                            item.get("namespace"),
                            item_type=item_type,
                        ) is not None
                        or (
                            item.get("namespace") is None
                            and self._has_unqualified_adapted_child(item.get("name"))
                        )
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                    else:
                        self._reject_unknown_standard_item(item, surface=surface)
                        decoded, item_changed = item, False
            elif item_type == "custom_tool_call":
                native_entry = self._native_entry_for_item(item)
                if native_entry is not None:
                    self._validate_native_item(item, native_entry)
                    decoded, item_changed = item, False
                elif self.has_adaptations and (
                    self._entry_for_name(
                        item.get("name"),
                        item.get("namespace"),
                        item_type=item_type,
                    ) is not None
                    or (
                        item.get("namespace") is None
                        and self._has_unqualified_adapted_child(item.get("name"))
                    )
                ):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
                else:
                    self._reject_unknown_standard_item(item, surface=surface)
                    decoded, item_changed = item, False
            elif item_type == "tool_search_call":
                native_entry = self._native_entry_for_item(item)
                if native_entry is not None:
                    self._validate_native_item(item, native_entry)
                    decoded, item_changed = item, False
                elif self._omitted_response_entry_for_item(item, owner=owner) is not None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface=surface,
                    )
                else:
                    self._reject_unknown_standard_item(item, surface=surface)
                    decoded, item_changed = item, False
            elif _hosted_kind_for_item_type(item_type) is not None:
                native_entry = self._native_entry_for_item(item)
                if native_entry is None:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="history")
                self._validate_native_item(item, native_entry, require_completed=True)
                decoded, item_changed = item, False
            elif isinstance(item_type, str) and item_type.endswith("_call"):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="history")
            elif item_type in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="history")
                if call_id in seen_result_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="history")
                seen_result_call_ids.add(call_id)
                decoded, item_changed = self._decode_result(item)
                if not item_changed and self.has_adaptations and not self._has_native_structural_family():
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_call_identity", surface="history")
                if item_type == "tool_search_output":
                    native_entry = self._native_entry_for_item(item)
                    if native_entry is not None:
                        self._validate_native_item(item, native_entry)
                    elif self.has_adaptations and not self._has_native_structural_family():
                        raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_call_identity", surface="history")
            elif _hosted_output_kind_for_item_type(item_type) is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface=surface,
                )
            else:
                decoded, item_changed = item, False
                if self.registry.looks_like_alias(item.get("name")):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="history")
            result.append(decoded)
            changed = changed or item_changed
        self._validate_collaboration_v2_items(result, surface=surface)
        return result, changed

    def decode_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = _copy_mapping(payload)
        for key in ("output", "input", "history"):
            if key in result:
                decoded, item_changed = self._decode_items(
                    result[key],
                    reject_omitted_response=key == "output",
                )
                if item_changed:
                    result[key] = decoded
        return result

    def encode_history(self, items: Iterable[Mapping[str, Any]]) -> list[Any]:
        payload = self.encode_payload({"input": list(items)})
        return payload.get("input", [])

    def decode_history(self, items: Iterable[Mapping[str, Any]]) -> list[Any]:
        payload = self.decode_payload({"input": list(items)})
        return payload.get("input", [])


@dataclass(slots=True)
class _PendingStreamItem:
    record: AliasRecord
    item_id: str
    call_id: str | None
    fragments: list[str] = field(default_factory=list)
    delta_done: bool = False
    item_done: bool = False


@dataclass(slots=True)
class _BufferedCustomStreamItem:
    record: AliasRecord
    added_event: dict[str, Any]
    item_id: str
    call_id: str
    fragments: list[str] = field(default_factory=list)
    arguments_done_event: dict[str, Any] | None = None
    native_input: str | None = None


@dataclass(slots=True)
class _BufferedToolSearchStreamItem:
    record: AliasRecord
    added_event: dict[str, Any]
    item_id: str
    call_id: str
    fragments: list[str] = field(default_factory=list)
    arguments_done_event: dict[str, Any] | None = None
    native_arguments: Any = None


@dataclass(slots=True)
class _OpaqueStreamItem:
    """Track a provider-local custom call without claiming a declaration.

    Some upstream Responses streams still expose the legacy ``custom_tool_call``
    lifecycle even when the request did not declare a structured custom tool.
    The wire item itself is the only owner available in that case.  Keep that
    owner opaque, but apply the same identity and terminal checks as declared
    calls.
    """

    item_id: str
    call_id: str
    wire_identity: tuple[Any, Any, Any]
    fragments: list[str] = field(default_factory=list)
    arguments_done: bool = False
    item_done: bool = False


@dataclass(slots=True)
class _HostedStreamState:
    event_kind: str
    stages: tuple[str, ...]
    next_stage: int = 0


class CompatibilityStreamState:
    """Assemble one adapted Responses SSE lifecycle before inverse mapping."""

    _TERMINAL_EVENT_TYPES = frozenset(
        {"response.completed", "response.incomplete", "response.failed"}
    )

    def __init__(self, plan: ToolCompatibilityPlan) -> None:
        self.plan = plan
        self._pending: dict[str, _PendingStreamItem] = {}
        self._seen_item_ids: set[str] = set()
        self._seen_call_ids: set[str] = set()
        self._native_pending: dict[str, tuple[str | None, ToolCompatibilityEntry]] = {}
        # The entry identifies the declaration, while this second ledger
        # retains the exact wire child/name that was observed at ``added``.
        # A namespace entry can contain several children, so family alone is
        # not sufficient to reconcile a terminal item.
        self._native_wire_identities: dict[str, tuple[Any, Any, Any]] = {}
        # Buffered custom adapters leave ``_pending`` once their native
        # lifecycle is emitted.  Keep the original adapter wire identity so a
        # terminal envelope can still reconcile item/call/declaration exactly.
        self._adapter_wire_identities: dict[str, tuple[str, AliasRecord, tuple[Any, Any, Any]]] = {}
        self._legacy_unowned_done: dict[
            str,
            tuple[str, ToolCompatibilityEntry, tuple[Any, Any, Any], Any],
        ] = {}
        self._wire_payloads: dict[str, Any] = {}
        self._native_done: set[str] = set()
        self._native_delta_done: set[str] = set()
        self._native_fragments: dict[str, list[str]] = {}
        self._hosted_pending: dict[str, _HostedStreamState] = {}
        self._buffered_custom: dict[str, _BufferedCustomStreamItem] = {}
        self._buffered_tool_search: dict[str, _BufferedToolSearchStreamItem] = {}
        # Provider-local custom calls have no declaration/alias owner.  Keep
        # an opaque stream owner once ``output_item.added`` establishes it so
        # later deltas and terminal events cannot borrow an arbitrary id.
        self._opaque_pending: dict[str, _OpaqueStreamItem] = {}
        self._terminal = False

    @property
    def terminal(self) -> bool:
        return self._terminal

    @staticmethod
    def _item_id(value: Mapping[str, Any]) -> str | None:
        item_id = value.get("item_id")
        plain_id = value.get("id")
        if (
            isinstance(item_id, str)
            and item_id
            and isinstance(plain_id, str)
            and plain_id
            and item_id != plain_id
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        for key in ("item_id", "id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return None

    def _pending_for(self, value: Mapping[str, Any]) -> _PendingStreamItem:
        item_id = self._item_id(value)
        if item_id and item_id in self._pending:
            return self._pending[item_id]
        raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")

    def _native_entry_for_item(self, item: Mapping[str, Any]) -> ToolCompatibilityEntry | None:
        return self.plan._native_entry_for_item(item)

    def _record_opaque_added(self, item: Mapping[str, Any]) -> _OpaqueStreamItem | None:
        """Record an undeclared legacy custom call established by ``added``."""
        if item.get("type") != "custom_tool_call":
            return None
        item_id = self._item_id(item)
        call_id = item.get("call_id")
        if item_id is None or not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_stream_identity",
                surface="stream",
            )
        if item_id in self._seen_item_ids or item_id in self._opaque_pending:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="stream",
            )
        if call_id in self._seen_call_ids or any(
            opaque.call_id == call_id for opaque in self._opaque_pending.values()
        ):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_call_identity",
                surface="stream",
            )
        opaque = _OpaqueStreamItem(
            item_id=item_id,
            call_id=call_id,
            wire_identity=self._native_wire_identity(item),
        )
        self._seen_item_ids.add(item_id)
        self._seen_call_ids.add(call_id)
        self._opaque_pending[item_id] = opaque
        return opaque

    @staticmethod
    def _opaque_payload(value: Any) -> tuple[str, Any]:
        return ("opaque_input", _freeze(value))

    def _record_legacy_unowned_native_done(
        self,
        item: Mapping[str, Any],
        entry: ToolCompatibilityEntry,
    ) -> None:
        """Record one complete legacy item that legitimately omits ``added``."""
        item_id = self._item_id(item)
        if item_id is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_item_identity",
                surface="stream",
            )
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_call_identity",
                surface="stream",
            )
        if item_id in self._seen_item_ids or item_id in self._native_done:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="stream",
            )
        if call_id in self._seen_call_ids:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_call_identity",
                surface="stream",
            )
        self.plan._validate_native_item(item, entry, surface="stream")
        self._seen_item_ids.add(item_id)
        self._seen_call_ids.add(call_id)
        self._native_done.add(item_id)
        self._legacy_unowned_done[item_id] = (
            call_id,
            entry,
            self._native_wire_identity(item),
            self._semantic_wire_payload(item, entry),
        )

    def _complete_native_arguments_from_item(
        self,
        item_id: str,
        item: Mapping[str, Any],
        entry: ToolCompatibilityEntry,
    ) -> None:
        """Accept a complete native item when its separate ``done`` event is omitted."""
        if entry.family == CUSTOM_FREEFORM:
            arguments = item.get("input")
            if not isinstance(arguments, str):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            fragments = self._native_fragments.get(item_id, [])
            if fragments and "".join(fragments) != arguments:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            payload_item = {"type": "custom_tool_call", "input": arguments}
        else:
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            fragments = self._native_fragments.get(item_id, [])
            if fragments and "".join(fragments) != arguments:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "incomplete_stream_delta",
                    surface="stream",
                )
            payload_item = {"type": "function_call", "arguments": arguments}
        payload = self._semantic_wire_payload(payload_item, entry)
        if item_id in self._wire_payloads and self._wire_payloads[item_id] != payload:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        self._wire_payloads[item_id] = payload
        self._native_delta_done.add(item_id)

    def _native_item_id_for_event(self, value: Mapping[str, Any]) -> str | None:
        item = value.get("item")
        nested_item_id = self._item_id(item) if isinstance(item, Mapping) else None
        event_item_id = self._item_id(value)
        if nested_item_id and event_item_id and nested_item_id != event_item_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
                surface="stream",
            )
        return nested_item_id or event_item_id

    @staticmethod
    def _native_wire_identity(item: Mapping[str, Any]) -> tuple[Any, Any, Any]:
        return (item.get("type"), item.get("name"), item.get("namespace"))

    @staticmethod
    def _semantic_wire_payload(
        item: Mapping[str, Any],
        owner: ToolCompatibilityEntry | AliasRecord,
    ) -> Any:
        """Return parsed call data used for terminal reconciliation.

        Equivalent JSON argument objects may differ in formatting or key
        order.  Custom adapters instead compare the envelope's actual input
        value.  Non-JSON arguments stay comparable as bounded raw strings;
        this check does not add a new validation rule.
        """
        item_type = item.get("type")
        family = owner.family
        if family == CUSTOM_FREEFORM:
            if item_type == "custom_tool_call":
                return ("custom_input", _freeze(item.get("input")))
            arguments = item.get("arguments")
            if arguments in (None, ""):
                return None
            try:
                envelope = _json_object_exact(arguments)
            except ToolCompatibilityError:
                return ("custom_arguments_raw", arguments)
            return ("custom_input", _freeze(envelope.get(CUSTOM_INPUT_KEY)))
        if family == TOOL_SEARCH and item_type == "function_call":
            arguments = item.get("arguments")
            if arguments in (None, ""):
                return None
            try:
                envelope = _json_object_with_key(arguments, TOOL_SEARCH_INPUT_KEY)
            except ToolCompatibilityError:
                return ("tool_search_arguments_raw", arguments)
            return ("arguments", _freeze(envelope[TOOL_SEARCH_INPUT_KEY]))
        if item_type == "function_call":
            arguments = item.get("arguments")
            if arguments in (None, ""):
                return None
            if isinstance(arguments, Mapping):
                return ("arguments", _freeze(arguments))
            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments)
                except (TypeError, ValueError):
                    return ("arguments_raw", arguments)
                return ("arguments", _freeze(parsed))
        if family == TOOL_SEARCH and item_type == "tool_search_call":
            return ("arguments", _freeze(item.get("arguments")))
        return None

    @staticmethod
    def _is_output_item_type(item_type: Any) -> bool:
        return isinstance(item_type, str) and (
            item_type in {
                "function_call_output",
                "custom_tool_call_output",
                "tool_search_output",
            }
            or item_type.endswith("_call_output")
        )

    def _validate_output_item_added(self, item: Mapping[str, Any]) -> None:
        """Require every stream output item to have a prior call owner."""
        if not self._is_output_item_type(item.get("type")):
            return
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_call_identity",
                surface="stream",
            )

        adapter_pending = next(
            (pending for pending in self._pending.values() if pending.call_id == call_id),
            None,
        )
        if adapter_pending is not None:
            record = self.plan.registry.record_for_call(call_id)
            if record is not adapter_pending.record:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
            expected = (
                "function_call_output"
                if adapter_pending.record.family in {NAMESPACE, CUSTOM_FREEFORM}
                else None
            )
            if expected is not None and item.get("type") != expected:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
            return

        native_pending = next(
            (
                (item_id, call_entry)
                for item_id, call_entry in self._native_pending.items()
                if call_entry[0] == call_id
            ),
            None,
        )
        if native_pending is not None:
            _item_id, (_call_id, entry) = native_pending
            expected = self.plan._history_output_type_for_entry(entry)
            if expected is not None and item.get("type") != expected:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
            return

        # A request-local registry may retain calls from a previously decoded
        # body/history.  It does not establish ownership for this SSE stream;
        # require the corresponding ``added`` event in this state.
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "missing_stream_identity",
            surface="stream",
        )

    def _check_alias_in_item(self, item: Mapping[str, Any], *, allow_incomplete: bool = True) -> tuple[dict[str, Any], AliasRecord | None, bool]:
        if item.get("type") == "function_call":
            decoded, record, changed = self.plan._decode_call_compat(item, allow_incomplete=allow_incomplete)
            if (
                record is not None
                and record.version == "v2"
                and record.family == NAMESPACE
                and not allow_incomplete
            ):
                try:
                    validate_collaboration_arguments(
                        COLLABORATION_V2,
                        str(record.child_name),
                        decoded.get("arguments"),
                    )
                except CollaborationContractError as exc:
                    self.plan._raise_collaboration_contract(exc, surface="stream")
            if record is not None and record.family == NAMESPACE:
                supplied_namespace = item.get("namespace")
                if supplied_namespace is not None and supplied_namespace != record.namespace:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
            entry = self.plan._entry_for_name(
                item.get("name"),
                item.get("namespace"),
                item_type=item.get("type"),
            )
            if record is None and self.plan._has_adapted_name_conflict(item.get("name"), PLAIN_FUNCTION):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="stream")
            if record is None and self.plan.has_adaptations and isinstance(item.get("name"), str) and (
                entry is None or entry.disposition == ADAPT
            ):
                raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="stream")
            return decoded, record, changed
        if self.plan.registry.looks_like_alias(item.get("name")):
            raise ToolCompatibilityError("tool_compatibility_boundary", "unknown_alias", surface="stream")
        return _copy_mapping(item), None, False

    def _decode_hosted_event(
        self,
        event: Mapping[str, Any],
        event_spec: tuple[str, str, tuple[str, ...]],
    ) -> dict[str, Any]:
        event_kind, stage, _stages = event_spec
        item_id = self._native_item_id_for_event(event)
        pending = self._hosted_pending.get(item_id) if item_id is not None else None
        if pending is None or pending.event_kind != event_kind:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "missing_stream_identity",
                surface="stream",
            )
        # Hosted stage events normally carry only ``item_id``.  If a provider
        # includes a nested item, however, its wire identity is part of the
        # lifecycle contract and must remain bound to the exact item observed
        # at ``output_item.added``.  Do not let a matching id borrow another
        # hosted kind or declaration.
        if "item" in event:
            nested_item = event.get("item")
            expected_wire = self._native_wire_identities.get(item_id)
            if (
                not isinstance(nested_item, Mapping)
                or expected_wire is None
                or self._native_wire_identity(nested_item) != expected_wire
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
        if item_id in self._native_done:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "duplicate_item_identity",
                surface="stream",
            )
        if pending.next_stage >= len(pending.stages) or pending.stages[pending.next_stage] != stage:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "invalid_hosted_lifecycle",
                surface="stream",
            )
        pending.next_stage += 1
        return _copy_mapping(event)

    def _finish_terminal(self) -> None:
        if self._terminal:
            raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_terminal", surface="stream")
        incomplete = [pending for pending in self._pending.values() if not pending.item_done]
        native_incomplete = any(
            item_id not in self._native_done
            or (
                entry.family not in {TOOL_SEARCH, SELECTED_PROVIDER_HOSTED}
                and item_id not in self._native_delta_done
            )
            for item_id, (_call_id, entry) in self._native_pending.items()
        )
        opaque_incomplete = any(
            not opaque.arguments_done or not opaque.item_done
            for opaque in self._opaque_pending.values()
        )
        search_incomplete = any(
            pending.arguments_done_event is None
            for pending in self._buffered_tool_search.values()
        )
        if incomplete or native_incomplete or opaque_incomplete or search_incomplete:
            raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream", surface="stream")
        self._terminal = True

    def _validate_terminal_native_output(self, response: Mapping[str, Any]) -> None:
        output = response.get("output")
        required_native_ids = {
            item_id
            for item_id in self._native_pending
        }
        required_adapter_ids = set(self._pending) | set(self._adapter_wire_identities)
        if output is None:
            # Some upstream terminal envelopes omit ``output`` after the SSE
            # lifecycle has already delivered the completed item.  _finish_terminal
            # below still rejects an actually incomplete lifecycle.
            return
        if output == []:
            if required_native_ids or required_adapter_ids:
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream", surface="stream")
            return
        if not isinstance(output, list):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_event", surface="stream")
        seen_output_ids: set[str] = set()
        for item in output:
            if not isinstance(item, Mapping):
                continue
            item_id = _item_identity(item)
            if item_id is not None:
                if item_id in seen_output_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                seen_output_ids.add(item_id)
            pending = self._native_pending.get(item_id) if item_id is not None else None
            if pending is None:
                legacy_done = self._legacy_unowned_done.get(item_id) if item_id is not None else None
                if legacy_done is not None:
                    expected_call_id, expected_entry, expected_wire, expected_payload = legacy_done
                    if item.get("call_id") != expected_call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != expected_wire:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if self._native_entry_for_item(item) is not expected_entry:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    self.plan._validate_native_item(item, expected_entry, surface="stream")
                    if self._semantic_wire_payload(item, expected_entry) != expected_payload:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    continue
                adapter_pending = self._pending.get(item_id) if item_id is not None else None
                if adapter_pending is not None:
                    if item.get("call_id") != adapter_pending.call_id:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
                    record = self.plan.registry.record_for_alias(item.get("name"))
                    if record is not adapter_pending.record:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
                    if (
                        adapter_pending.record.family == NAMESPACE
                        and item.get("namespace") not in {None, adapter_pending.record.namespace}
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    if (
                        item_id in self._wire_payloads
                        and self._semantic_wire_payload(item, adapter_pending.record)
                        != self._wire_payloads[item_id]
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    continue
                adapter_identity = self._adapter_wire_identities.get(item_id) if item_id is not None else None
                if adapter_identity is not None:
                    expected_call_id, expected_record, expected_wire = adapter_identity
                    if item.get("call_id") != expected_call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != expected_wire:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if (
                        item_id in self._wire_payloads
                        and self._semantic_wire_payload(item, expected_record)
                        != self._wire_payloads[item_id]
                    ):
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    terminal_record = self.plan.registry.record_for_alias(item.get("name"))
                    if terminal_record is not expected_record:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                    )
                    continue
                opaque = self._opaque_pending.get(item_id) if item_id is not None else None
                if opaque is not None:
                    if item.get("call_id") != opaque.call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != opaque.wire_identity:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if "input" in item:
                        terminal_input = item.get("input")
                    elif "arguments" in item:
                        terminal_input = item.get("arguments")
                    else:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "incomplete_stream_delta",
                            surface="stream",
                        )
                    payload = self._opaque_payload(terminal_input)
                    if (
                        item_id in self._wire_payloads
                        and self._wire_payloads[item_id] != payload
                    ):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    continue
                if isinstance(item.get("call_id"), str) and any(
                    expected_call_id == item.get("call_id")
                    for expected_call_id, _entry, _wire, _payload in self._legacy_unowned_done.values()
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if isinstance(item.get("call_id"), str) and any(
                    expected_call_id == item.get("call_id")
                    for expected_call_id, _record, _wire in self._adapter_wire_identities.values()
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                bound_record = self.plan.registry.record_for_call(item.get("call_id"))
                if bound_record is not None:
                    terminal_record = self.plan.registry.record_for_alias(item.get("name"))
                    if terminal_record is not bound_record:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    # A buffered custom adapter is removed from _pending once
                    # its native events are emitted.  Its request-local call
                    # binding is still sufficient to verify the terminal
                    # alias, so let the exact match through.
                    continue
                if isinstance(item.get("call_id"), str) and any(
                    opaque.call_id == item.get("call_id")
                    for opaque in self._opaque_pending.values()
                ):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                # Adapter output is accounted for by the request-scoped alias
                # ledger; any other native-shaped item must match a stream
                # owner before it can appear in the terminal envelope.
                if self._native_entry_for_item(item) is not None:
                    # A complete body-style terminal envelope is valid even
                    # when the caller did not expose the preceding SSE
                    # ``added`` event.  Once this stream has a pending tool,
                    # however, a native item without its exact owner is a
                    # mismatched terminal identity.
                    if self._native_pending or self._pending:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                continue
            expected_call_id, expected_entry = pending
            native_entry = self._native_entry_for_item(item)
            if native_entry is not expected_entry:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
            if expected_entry.family == TOOL_SEARCH:
                self.plan._validate_native_item(item, expected_entry, surface="stream")
            if self._native_wire_identities.get(item_id) != self._native_wire_identity(item):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
            if (
                item_id in self._wire_payloads
                and self._semantic_wire_payload(item, expected_entry)
                != self._wire_payloads[item_id]
            ):
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
            if expected_call_id is not None and item.get("call_id") != expected_call_id:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_call_identity",
                    surface="stream",
                )
        missing = (required_native_ids | required_adapter_ids) - seen_output_ids
        if missing:
            raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream", surface="stream")

    def decode_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_event", surface="stream")
        result = _copy_mapping(event)
        event_type = result.get("type")
        self._reject_after_terminal(event_type)
        if _is_unsupported_hosted_stream_event(event_type):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unsupported_hosted_lifecycle",
                surface="stream",
            )
        hosted_event_spec = _hosted_event_spec(event_type)
        if hosted_event_spec is not None:
            return self._decode_hosted_event(result, hosted_event_spec)
        item = result.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            self.plan._validate_collaboration_v2_items([item], surface="stream")
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            response = result.get("response")
            if isinstance(response, Mapping):
                self._validate_terminal_native_output(response)
            self._finish_terminal()
            if isinstance(response, Mapping):
                decoded_output, output_changed = self.plan._decode_items(
                    response.get("output"),
                    reject_omitted_response=True,
                )
                if output_changed:
                    response = _copy_mapping(response)
                    response["output"] = decoded_output
                    result["response"] = response
            return result
        if event_type == "response.output_item.added" and isinstance(item, Mapping):
            self.plan._validate_registered_item_identity(item, surface="stream")
            self._validate_output_item_added(item)
            self.plan._reject_unknown_standard_item(item, surface="stream")
            decoded_item, record, _changed = self._check_alias_in_item(item)
            item_id = self._item_id(item)
            if record is not None:
                if not item_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_item_identity", surface="stream")
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="stream")
                if item_id in self._seen_item_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                self._seen_item_ids.add(item_id)
                if call_id in self._seen_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="stream")
                self._seen_call_ids.add(call_id)
                if self.plan.registry.record_for_call(call_id) is None:
                    self.plan.registry.bind_call(call_id, record.alias)
                self._pending[item_id] = _PendingStreamItem(record, item_id, call_id if isinstance(call_id, str) else None)
                self._adapter_wire_identities[item_id] = (
                    call_id,
                    record,
                    self._native_wire_identity(item),
                )
                result["item"] = decoded_item
                return result
            native_entry = self._native_entry_for_item(item)
            if native_entry is not None:
                item_id = self._item_id(item)
                call_id = item.get("call_id")
                if item_id is None:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_item_identity", surface="stream")
                if native_entry.family == SELECTED_PROVIDER_HOSTED:
                    self.plan._validate_native_item(item, native_entry, surface="stream")
                    if item_id in self._seen_item_ids:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                    self._seen_item_ids.add(item_id)
                    self._native_pending[item_id] = (None, native_entry)
                    self._native_wire_identities[item_id] = self._native_wire_identity(item)
                    hosted_spec = _hosted_event_spec_for_declaration_kind(
                        native_entry.declaration.get("type")
                    )
                    if hosted_spec is None:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "unsupported_hosted_lifecycle",
                            surface="stream",
                        )
                    self._hosted_pending[item_id] = _HostedStreamState(
                        event_kind=hosted_spec[0],
                        stages=hosted_spec[1],
                    )
                    result["item"] = decoded_item
                    return result
                if not isinstance(call_id, str) or not call_id:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_call_identity", surface="stream")
                self.plan._validate_native_item(item, native_entry, surface="stream")
                if item_id in self._seen_item_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                if call_id in self._seen_call_ids:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_call_identity", surface="stream")
                self._seen_item_ids.add(item_id)
                self._seen_call_ids.add(call_id)
                self._native_pending[item_id] = (call_id, native_entry)
                self._native_wire_identities[item_id] = self._native_wire_identity(item)
                result["item"] = decoded_item
            elif item.get("type") == "custom_tool_call":
                if self.plan._omitted_response_entry_for_item(item) is not None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                adapted_entry = (
                    self.plan._entry_for_name(
                        item.get("name"),
                        item.get("namespace"),
                        item_type=item.get("type"),
                    )
                    if self.plan.has_adaptations
                    else None
                )
                if adapted_entry is not None and adapted_entry.disposition == ADAPT:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
                self._record_opaque_added(item)
            elif self.plan.has_adaptations and item.get("type") == "function_call":
                adapted_entry = self.plan._entry_for_name(
                    item.get("name"),
                    item.get("namespace"),
                    item_type=item.get("type"),
                )
                if adapted_entry is not None and adapted_entry.disposition == ADAPT:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
            elif self.plan._omitted_response_entry_for_item(item) is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface="stream",
                )
            elif _hosted_kind_for_item_type(item.get("type")) is not None or (
                isinstance(item.get("type"), str)
                and item.get("type").endswith("_call")
                and item.get("type")
                not in {"function_call", "custom_tool_call", "tool_search_call"}
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unsupported_hosted_lifecycle",
                    surface="stream",
                )
            return result
        if event_type in {"response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}:
            item_id = self._item_id(result)
            if item_id in self._native_pending:
                expected_call_id, expected_entry = self._native_pending[item_id]
                if expected_entry.family == TOOL_SEARCH:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != expected_call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                delta = result.get("delta")
                if not isinstance(delta, str):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_delta", surface="stream")
                if item_id in self._native_delta_done:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_stream_done", surface="stream")
                self._native_fragments.setdefault(item_id, []).append(delta)
                return result
            opaque = self._opaque_pending.get(item_id) if item_id is not None else None
            if opaque is not None:
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != opaque.call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if opaque.arguments_done or opaque.item_done:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_stream_done",
                        surface="stream",
                    )
                delta = result.get("delta")
                if not isinstance(delta, str):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "malformed_stream_delta",
                        surface="stream",
                    )
                opaque.fragments.append(delta)
                return result
            if item_id not in self._pending:
                raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
            pending = self._pending_for(result)
            if isinstance(result.get("call_id"), str) and result.get("call_id") != pending.call_id:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
            delta = result.get("delta")
            if not isinstance(delta, str):
                raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_stream_delta", surface="stream")
            pending.fragments.append(delta)
            if pending.record.family == CUSTOM_FREEFORM:
                result["type"] = "response.custom_tool_call_input.delta"
            return result
        if event_type in {"response.function_call_arguments.done", "response.custom_tool_call_input.done"}:
            item_id = self._item_id(result)
            if item_id in self._native_pending:
                expected_call_id, expected_entry = self._native_pending[item_id]
                if expected_entry.family == TOOL_SEARCH:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != expected_call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if item_id in self._native_delta_done:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_stream_done", surface="stream")
                arguments = result.get("arguments", result.get("input"))
                if not isinstance(arguments, str):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
                fragments = self._native_fragments.get(item_id, [])
                if fragments and "".join(fragments) != arguments:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
                payload_item = (
                    {"type": "custom_tool_call", "input": result.get("input", arguments)}
                    if expected_entry.family == CUSTOM_FREEFORM
                    else {"type": "function_call", "arguments": arguments}
                )
                payload = self._semantic_wire_payload(payload_item, expected_entry)
                if item_id in self._wire_payloads and self._wire_payloads[item_id] != payload:
                    raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                self._wire_payloads[item_id] = payload
                self._native_delta_done.add(item_id)
                return result
            opaque = self._opaque_pending.get(item_id) if item_id is not None else None
            if opaque is not None:
                supplied_call_id = result.get("call_id")
                if supplied_call_id is not None and supplied_call_id != opaque.call_id:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "ambiguous_call_identity",
                        surface="stream",
                    )
                if opaque.arguments_done:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "duplicate_stream_done",
                        surface="stream",
                    )
                arguments = result.get("arguments", result.get("input"))
                if not isinstance(arguments, str):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "incomplete_stream_delta",
                        surface="stream",
                    )
                fragments = "".join(opaque.fragments)
                if fragments and not arguments.startswith(fragments):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "incomplete_stream_delta",
                        surface="stream",
                    )
                opaque.arguments_done = True
                self._wire_payloads[item_id] = self._opaque_payload(arguments)
                return result
            if item_id not in self._pending:
                raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
            pending = self._pending_for(result)
            if isinstance(result.get("call_id"), str) and result.get("call_id") != pending.call_id:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
            if pending.delta_done:
                raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_stream_done", surface="stream")
            arguments = result.get("arguments", result.get("input"))
            if not isinstance(arguments, str):
                arguments = "".join(pending.fragments)
            elif pending.fragments and "".join(pending.fragments) != arguments:
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
            if not arguments:
                raise ToolCompatibilityError("tool_compatibility_boundary", "incomplete_stream_delta", surface="stream")
            pending.delta_done = True
            if pending.record.family == CUSTOM_FREEFORM:
                envelope = _json_object_exact(arguments)
                result["type"] = "response.custom_tool_call_input.done"
                result["input"] = envelope[CUSTOM_INPUT_KEY]
                result.pop("arguments", None)
            else:
                result["arguments"] = arguments
            payload_item = (
                {"type": "custom_tool_call", "input": result.get("input")}
                if pending.record.family == CUSTOM_FREEFORM
                else {"type": "function_call", "arguments": arguments}
            )
            payload = self._semantic_wire_payload(payload_item, pending.record)
            if item_id in self._wire_payloads and self._wire_payloads[item_id] != payload:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
            self._wire_payloads[item_id] = payload
            return result
        if event_type == "response.output_item.done" and isinstance(item, Mapping):
            # A legacy provider may omit the ``added`` lifecycle and emit the
            # adapted client-owned search as a plain function call.  Decode
            # that exact spelling before the normal registry/stream-owner
            # checks so the client still receives the native terminal search
            # item and the provider cannot claim an arbitrary function.
            legacy_record = self.plan._legacy_tool_search_record(item)
            if legacy_record is not None:
                decoded_item, _record, _changed = self.plan._decode_call_compat(
                    item,
                    allow_incomplete=False,
                )
                entry = next(
                    (
                        candidate
                        for candidate in self.plan.entries
                        if candidate.declaration_index == legacy_record.declaration_index
                    ),
                    None,
                )
                if entry is None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
                self._record_legacy_unowned_native_done(decoded_item, entry)
                result["item"] = decoded_item
                return result
            self.plan._validate_registered_item_identity(item, surface="stream")
            self._validate_output_item_added(item)
            self.plan._reject_unknown_standard_item(item, surface="stream")
            record = self.plan.registry.record_for_alias(item.get("name"))
            if record is None:
                if self.plan.registry.looks_like_alias(item.get("name")):
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unknown_alias",
                        surface="stream",
                    )
                native_item_id = self._item_id(item)
                opaque = self._opaque_pending.get(native_item_id) if native_item_id else None
                if opaque is not None:
                    if item.get("call_id") != opaque.call_id:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_call_identity",
                            surface="stream",
                        )
                    if self._native_wire_identity(item) != opaque.wire_identity:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if not opaque.arguments_done:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "incomplete_stream_delta",
                            surface="stream",
                        )
                    if opaque.item_done:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "duplicate_item_identity",
                            surface="stream",
                        )
                    if "input" in item:
                        terminal_input = item.get("input")
                    elif "arguments" in item:
                        terminal_input = item.get("arguments")
                    else:
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "incomplete_stream_delta",
                            surface="stream",
                        )
                    payload = self._opaque_payload(terminal_input)
                    if (
                        native_item_id in self._wire_payloads
                        and self._wire_payloads[native_item_id] != payload
                    ):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    opaque.item_done = True
                    self._wire_payloads[native_item_id] = payload
                    return result
                native_pending = self._native_pending.get(native_item_id) if native_item_id else None
                if native_pending is not None:
                    call_id = item.get("call_id")
                    if call_id != native_pending[0]:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
                    expected_entry = native_pending[1]
                    if self._native_wire_identities.get(native_item_id) != self._native_wire_identity(item):
                        raise ToolCompatibilityError(
                            "tool_compatibility_boundary",
                            "ambiguous_native_identity",
                            surface="stream",
                        )
                    if expected_entry.family == SELECTED_PROVIDER_HOSTED:
                        hosted_entry = self._native_entry_for_item(item)
                        if hosted_entry is None or hosted_entry is not expected_entry:
                            raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                        hosted_state = self._hosted_pending.get(native_item_id)
                        if hosted_state is None or hosted_state.next_stage != len(hosted_state.stages):
                            raise ToolCompatibilityError(
                                "tool_compatibility_boundary",
                                "incomplete_hosted_lifecycle",
                                surface="stream",
                            )
                        self.plan._validate_native_item(
                            item,
                            hosted_entry,
                            require_completed=True,
                            surface="stream",
                        )
                    elif expected_entry.family == TOOL_SEARCH:
                        self.plan._validate_native_item(item, expected_entry, surface="stream")
                    elif expected_entry.family != TOOL_SEARCH and native_item_id not in self._native_delta_done:
                        self._complete_native_arguments_from_item(
                            native_item_id,
                            item,
                            expected_entry,
                        )
                    if native_item_id in self._native_done:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
                    payload = self._semantic_wire_payload(item, expected_entry)
                    if native_item_id in self._wire_payloads and self._wire_payloads[native_item_id] != payload:
                        raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
                    self._wire_payloads[native_item_id] = payload
                    self._native_done.add(native_item_id)
                    return result
                native_entry = self._native_entry_for_item(item)
                if native_entry is not None:
                    if (
                        native_entry.family == TOOL_SEARCH
                        and item.get("type") == "tool_search_call"
                    ):
                        self._record_legacy_unowned_native_done(item, native_entry)
                        return result
                    if (
                        native_entry.family == PLAIN_FUNCTION
                        and native_entry.original_name == "tool_search"
                        and item.get("type") == "function_call"
                        and item.get("name") == "tool_search"
                        and item.get("namespace") is None
                    ):
                        self._record_legacy_unowned_native_done(item, native_entry)
                        return result
                    if (
                        native_entry.family == PLAIN_FUNCTION
                        and native_entry.original_name == "multi_agent_v1__spawn_agent"
                        and item.get("type") == "function_call"
                        and item.get("namespace") == "multi_agent_v1"
                        and item.get("name") == "spawn_agent"
                    ):
                        # A legacy worker-selector stream can emit this
                        # flattened native item without an ``added`` event.
                        # Keep this one compatibility passthrough narrow; a
                        # plain unqualified native call still requires an
                        # established stream owner.
                        self._record_legacy_unowned_native_done(item, native_entry)
                        return result
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
                if item.get("type") == "custom_tool_call":
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "missing_stream_identity",
                        surface="stream",
                    )
                if self.plan._omitted_response_entry_for_item(item) is not None:
                    raise ToolCompatibilityError(
                        "tool_compatibility_boundary",
                        "unsupported_hosted_lifecycle",
                        surface="stream",
                    )
                if _hosted_kind_for_item_type(item.get("type")) is not None or (
                    isinstance(item.get("type"), str)
                    and item.get("type").endswith("_call")
                    and item.get("type")
                    not in {"function_call", "custom_tool_call", "tool_search_call"}
                ):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "unsupported_hosted_lifecycle", surface="stream")
                # A Responses stream can contain ordinary assistant messages
                # (and other non-tool output items) alongside adapted tool
                # lifecycles.  Their ``output_item.done`` event is complete
                # on its own and has no runtime-tool owner to reconcile.
                # Only reject an otherwise-unowned item when it is a
                # tool-shaped lifecycle that could cross the compatibility
                # boundary ambiguously.
                if native_item_id is not None and self.plan.has_adaptations and (
                    isinstance(item.get("type"), str)
                    and (
                        item.get("type") in {"function_call", "custom_tool_call", "tool_search_call"}
                        or item.get("type").endswith("_call")
                        or item.get("type").endswith("_call_output")
                    )
                ):
                    raise ToolCompatibilityError("tool_compatibility_boundary", "missing_stream_identity", surface="stream")
                return result
            pending = self._pending_for(item)
            if (
                pending.record.family == CUSTOM_FREEFORM
                and self._native_wire_identity(item)
                != self._adapter_wire_identities[pending.item_id][2]
            ):
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "ambiguous_native_identity",
                    surface="stream",
                )
            if item.get("call_id") != pending.call_id:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_call_identity", surface="stream")
            if pending.item_done:
                raise ToolCompatibilityError("tool_compatibility_boundary", "duplicate_item_identity", surface="stream")
            if pending.record.family == CUSTOM_FREEFORM:
                decoded_item, _record, _changed = self.plan._decode_call(item, allow_incomplete=False)
            else:
                decoded_item, _record, _changed = self._check_alias_in_item(item, allow_incomplete=False)
            pending.item_done = True
            payload = self._semantic_wire_payload(item, pending.record)
            if pending.item_id in self._wire_payloads and self._wire_payloads[pending.item_id] != payload:
                raise ToolCompatibilityError("tool_compatibility_boundary", "ambiguous_native_identity", surface="stream")
            self._wire_payloads[pending.item_id] = payload
            result["item"] = decoded_item
            return result
        if isinstance(item, Mapping) and self.plan.registry.looks_like_alias(item.get("name")):
            decoded_item, _record, _changed = self._check_alias_in_item(item)
            result["item"] = decoded_item
        return result

    @staticmethod
    def _stream_error(classification: str) -> ToolCompatibilityError:
        return ToolCompatibilityError(
            "tool_compatibility_boundary",
            classification,
            surface="stream",
        )

    def _reject_after_terminal(self, event_type: Any) -> None:
        if self._terminal and event_type not in self._TERMINAL_EVENT_TYPES:
            raise self._stream_error("stream_after_terminal")

    @staticmethod
    def _native_custom_item(
        item: Mapping[str, Any],
        record: AliasRecord,
        native_input: str,
    ) -> dict[str, Any]:
        result = _copy_mapping(item)
        result["type"] = "custom_tool_call"
        result["name"] = record.original_name
        result["input"] = native_input
        result.pop("arguments", None)
        return result

    @staticmethod
    def _native_tool_search_item(
        item: Mapping[str, Any],
        native_arguments: Any,
    ) -> dict[str, Any]:
        """Convert an adapted function-call item back to tool_search."""
        result = _copy_mapping(item)
        result["type"] = "tool_search_call"
        result.pop("name", None)
        result.pop("namespace", None)
        # ``status`` is a Chat/Responses function-call field.  Codex CLI's
        # native client-owned tool_search item does not carry it; retaining
        # the adapted transport marker makes the item fail the native shape
        # check before MCP discovery.
        result.pop("status", None)
        result["execution"] = "client"
        result["arguments"] = {} if native_arguments is None else _thaw(native_arguments)
        return result

    def decode_events_for_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(event, Mapping):
            raise self._stream_error("malformed_stream_event")
        value = _copy_mapping(event)
        event_type = value.get("type")
        self._reject_after_terminal(event_type)
        item = value.get("item")

        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            if self._buffered_custom or self._buffered_tool_search:
                raise self._stream_error("incomplete_stream")
            return [self.decode_event(value)]

        if event_type == "response.output_item.added" and isinstance(item, Mapping):
            self.plan._validate_registered_item_identity(item, surface="stream")
            self.plan._reject_unknown_standard_item(item, surface="stream")
            record = self.plan.registry.record_for_alias(item.get("name"))
            if record is None:
                record = self.plan._legacy_tool_search_record(item)
            if record is None or record.family not in {CUSTOM_FREEFORM, TOOL_SEARCH}:
                return [self.decode_event(value)]
            if record.family == CUSTOM_FREEFORM:
                if item.get("type") != "function_call":
                    raise self._stream_error("ambiguous_call_identity")
                item_id = self._item_id(item)
                call_id = item.get("call_id")
                if (
                    not item_id
                    or not isinstance(call_id, str)
                    or not call_id
                    or item_id in self._seen_item_ids
                    or call_id in self._seen_call_ids
                    or item.get("arguments") not in {None, ""}
                    or item.get("namespace") is not None
                ):
                    raise self._stream_error("invalid_custom_stream_identity")
                self._seen_item_ids.add(item_id)
                self._seen_call_ids.add(call_id)
                if self.plan.registry.record_for_call(call_id) is None:
                    self.plan.registry.bind_call(call_id, record.alias)
                self._buffered_custom[item_id] = _BufferedCustomStreamItem(
                    record=record,
                    added_event=value,
                    item_id=item_id,
                    call_id=call_id,
                )
                self._adapter_wire_identities[item_id] = (
                    call_id,
                    record,
                    self._native_wire_identity(item),
                )
                return []

        if event_type == "response.output_item.added" and isinstance(item, Mapping):
            record = self.plan.registry.record_for_alias(item.get("name"))
            if record is None:
                legacy_entry = self.plan._legacy_tool_search_record(item)
                if legacy_entry is not None and legacy_entry.aliases:
                    record = self.plan.registry.record_for_alias(legacy_entry.aliases[0])
            if record is not None and record.family == TOOL_SEARCH:
                if item.get("type") != "function_call":
                    raise self._stream_error("ambiguous_call_identity")
                item_id = self._item_id(item)
                call_id = item.get("call_id")
                if (
                    not item_id
                    or not isinstance(call_id, str)
                    or not call_id
                    or item_id in self._seen_item_ids
                    or call_id in self._seen_call_ids
                    or item.get("arguments") not in {None, ""}
                    or item.get("namespace") is not None
                ):
                    raise self._stream_error("invalid_tool_search_stream_identity")
                self._seen_item_ids.add(item_id)
                self._seen_call_ids.add(call_id)
                if self.plan.registry.record_for_call(call_id) is None:
                    self.plan.registry.bind_call(call_id, record.alias)
                self._buffered_tool_search[item_id] = _BufferedToolSearchStreamItem(
                    record=record,
                    added_event=value,
                    item_id=item_id,
                    call_id=call_id,
                )
                self._adapter_wire_identities[item_id] = (
                    call_id,
                    record,
                    self._native_wire_identity(item),
                )
                return []

        item_id = self._item_id(value)
        pending = self._buffered_custom.get(item_id) if item_id else None
        if event_type == "response.function_call_arguments.delta" and pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            delta = value.get("delta")
            if not isinstance(delta, str) or pending.arguments_done_event is not None:
                raise self._stream_error("malformed_stream_delta")
            pending.fragments.append(delta)
            return []

        if event_type == "response.function_call_arguments.done" and pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            if pending.arguments_done_event is not None:
                raise self._stream_error("duplicate_stream_done")
            arguments = value.get("arguments")
            if not isinstance(arguments, str) or (
                pending.fragments and "".join(pending.fragments) != arguments
            ):
                raise self._stream_error("incomplete_stream_delta")
            try:
                envelope = _json_object_exact(arguments)
            except ToolCompatibilityError as exc:
                raise self._stream_error(exc.classification) from exc
            native_input = envelope[CUSTOM_INPUT_KEY]
            if not isinstance(native_input, str):
                raise self._stream_error("malformed_stream_custom_input")
            pending.arguments_done_event = value
            pending.native_input = native_input
            return []

        search_pending = self._buffered_tool_search.get(item_id) if item_id else None
        if event_type == "response.function_call_arguments.delta" and search_pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != search_pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            delta = value.get("delta")
            if not isinstance(delta, str) or search_pending.arguments_done_event is not None:
                raise self._stream_error("malformed_stream_delta")
            search_pending.fragments.append(delta)
            return []

        if event_type == "response.function_call_arguments.done" and search_pending is not None:
            supplied_call_id = value.get("call_id")
            if supplied_call_id is not None and supplied_call_id != search_pending.call_id:
                raise self._stream_error("ambiguous_call_identity")
            if search_pending.arguments_done_event is not None:
                raise self._stream_error("duplicate_stream_done")
            arguments = value.get("arguments")
            if not isinstance(arguments, str) or (
                search_pending.fragments and "".join(search_pending.fragments) != arguments
            ):
                raise self._stream_error("incomplete_stream_delta")
            try:
                envelope = _json_object_with_key(arguments, TOOL_SEARCH_INPUT_KEY)
            except ToolCompatibilityError as exc:
                # Legacy providers use the original ``tool_search`` name and
                # send the native query object directly instead of the
                # adapter envelope.  Accept only that request-bound spelling;
                # an aliased function must still carry the exact envelope.
                added_item = search_pending.added_event.get("item")
                legacy_wire = isinstance(added_item, Mapping) and added_item.get("name") == "tool_search"
                if not legacy_wire:
                    raise self._stream_error(exc.classification) from exc
                try:
                    native_arguments = json.loads(arguments)
                except (TypeError, ValueError) as parse_error:
                    raise self._stream_error("malformed_envelope") from parse_error
                if not isinstance(native_arguments, Mapping):
                    raise self._stream_error("invalid_envelope")
                envelope = {TOOL_SEARCH_INPUT_KEY: native_arguments}
            search_pending.arguments_done_event = value
            search_pending.native_arguments = envelope[TOOL_SEARCH_INPUT_KEY]
            # Preserve the legacy provider's plain arguments-done event.  The
            # outer bounded-query guard can then suppress only the repeated
            # query while allowing a distinct search to remain visible; the
            # adapted alias envelope itself is still kept provider-local.
            added_item = search_pending.added_event.get("item")
            if isinstance(added_item, Mapping) and added_item.get("name") == "tool_search":
                return [value]
            return []

        if event_type == "response.output_item.done" and isinstance(item, Mapping):
            done_item_id = self._item_id(item)
            pending = self._buffered_custom.get(done_item_id) if done_item_id else None
            if pending is not None:
                expected_wire = self._adapter_wire_identities.get(pending.item_id)
                if (
                    expected_wire is None
                    or self._native_wire_identity(item) != expected_wire[2]
                ):
                    raise self._stream_error("ambiguous_native_identity")
                if (
                    pending.arguments_done_event is None
                    or pending.native_input is None
                    or item.get("call_id") != pending.call_id
                    or item.get("name") != pending.record.alias
                    or item.get("arguments") != pending.arguments_done_event.get("arguments")
                ):
                    raise self._stream_error("incomplete_stream")

                added_event = _copy_mapping(pending.added_event)
                added_event["item"] = self._native_custom_item(
                    added_event["item"],
                    pending.record,
                    "",
                )

                delta_event = _copy_mapping(pending.arguments_done_event)
                delta_event["type"] = "response.custom_tool_call_input.delta"
                delta_event["delta"] = pending.native_input
                delta_event.pop("arguments", None)

                input_done_event = _copy_mapping(pending.arguments_done_event)
                input_done_event["type"] = "response.custom_tool_call_input.done"
                input_done_event["input"] = pending.native_input
                input_done_event.pop("arguments", None)

                value["item"] = self._native_custom_item(
                    item,
                    pending.record,
                    pending.native_input,
                )
                self._wire_payloads[pending.item_id] = self._semantic_wire_payload(item, pending.record)
                del self._buffered_custom[pending.item_id]
                return [added_event, delta_event, input_done_event, value]

        if event_type == "response.output_item.done" and isinstance(item, Mapping):
            done_item_id = self._item_id(item)
            search_pending = self._buffered_tool_search.get(done_item_id) if done_item_id else None
            if search_pending is None:
                return [self.decode_event(value)]
            expected_wire = self._adapter_wire_identities.get(search_pending.item_id)
            if (
                expected_wire is None
                or self._native_wire_identity(item) != expected_wire[2]
                or search_pending.arguments_done_event is None
                or item.get("call_id") != search_pending.call_id
                or item.get("name") not in {search_pending.record.alias, "tool_search"}
                or item.get("arguments") != search_pending.arguments_done_event.get("arguments")
            ):
                raise self._stream_error("incomplete_stream")
            native_added = _copy_mapping(search_pending.added_event)
            native_added.pop("output_index", None)
            native_added["item"] = self._native_tool_search_item(
                native_added["item"],
                search_pending.native_arguments,
            )
            value = _copy_mapping(value)
            value.pop("output_index", None)
            value["item"] = self._native_tool_search_item(item, search_pending.native_arguments)
            self._wire_payloads[search_pending.item_id] = self._semantic_wire_payload(
                item,
                search_pending.record,
            )
            self._native_done.add(search_pending.item_id)
            del self._buffered_tool_search[search_pending.item_id]
            # Codex CLI 0.146 consumes the client-owned search lifecycle as
            # a terminal output item.  The adapted function envelope is an
            # upstream-only transport detail; forwarding an ``added`` event
            # causes the native client to treat the search call as an
            # ordinary provider function and skip MCP discovery.  Preserve
            # the state transition above, but expose only the native terminal
            # item downstream.
            return [value]

        return [self.decode_event(value)]

    def decode_events(self, events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        decoded: list[dict[str, Any]] = []
        for event in events:
            decoded.extend(self.decode_events_for_event(event))
        return decoded


# Short aliases used by route/request callers and by compatibility-focused
# tests.  They intentionally all construct the same immutable plan.
build_plan = build_tool_compatibility_plan
ToolPlan = ToolCompatibilityPlan
AliasRegistry = RequestScopedToolAliasRegistry


__all__ = [
    "ADAPT",
    "AliasRecord",
    "AliasRegistry",
    "CompatibilityDiagnostics",
    "CompatibilityStreamState",
    "CUSTOM_FREEFORM",
    "CUSTOM_INPUT_KEY",
    "CUSTOM_OUTPUT_KEY",
    "TOOL_SEARCH_INPUT_KEY",
    "TOOL_SEARCH_OUTPUT_KEY",
    "HostedCapabilityFacts",
    "NAMESPACE",
    "NATIVE",
    "OMIT",
    "PLAIN_FUNCTION",
    "ProtocolCapabilities",
    "REQUIRED_BUT_UNAVAILABLE",
    "RequiredToolUnavailableError",
    "RequestScopedToolAliasRegistry",
    "SELECTED_PROVIDER_HOSTED",
    "TOOL_SEARCH",
    "ToolCompatibilityEntry",
    "ToolCompatibilityError",
    "ToolCompatibilityPlan",
    "ToolPlan",
    "UNKNOWN_FUTURE_KIND",
    "build_plan",
    "build_tool_compatibility_plan",
    "classify_declaration",
]
