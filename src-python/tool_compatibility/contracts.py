"""Capability tables and bounded compatibility errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


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


def _protocol_capabilities(
    selected_protocol: str,
    supplied: ProtocolCapabilities | Mapping[str, Any] | None,
) -> ProtocolCapabilities:
    if isinstance(supplied, ProtocolCapabilities):
        return supplied
    return ProtocolCapabilities.for_protocol(selected_protocol, supplied)


def _provider_hosted(value: Any) -> HostedCapabilityFacts:
    return HostedCapabilityFacts.from_value(value)
