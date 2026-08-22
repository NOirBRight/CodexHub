"""Request-scoped runtime tool compatibility for the selected Gateway route.

This package deliberately knows only about declaration shape and protocol
capabilities.  It does not select a Provider, execute a tool, or repair a
model response.  The request plan is immutable; the small mutable stream
object is the lifecycle ledger used while assembling one SSE response.
"""

from .contracts import (
    HostedCapabilityFacts,
    ProtocolCapabilities,
    RequiredToolUnavailableError,
    ToolCompatibilityError,
)
from .dispositions import (
    ADAPT,
    CUSTOM_FREEFORM,
    NAMESPACE,
    NATIVE,
    OMIT,
    PLAIN_FUNCTION,
    REQUIRED_BUT_UNAVAILABLE,
    SELECTED_PROVIDER_HOSTED,
    TOOL_SEARCH,
    UNKNOWN_FUTURE_KIND,
    classify_declaration,
)
from .plan import (
    CUSTOM_INPUT_KEY,
    CUSTOM_OUTPUT_KEY,
    TOOL_SEARCH_INPUT_KEY,
    TOOL_SEARCH_OUTPUT_KEY,
    AliasRecord,
    AliasRegistry,
    CompatibilityDiagnostics,
    CompatibilityStreamState,
    RequestScopedToolAliasRegistry,
    ToolCompatibilityEntry,
    ToolCompatibilityPlan,
    ToolPlan,
    build_plan,
    build_tool_compatibility_plan,
)

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
