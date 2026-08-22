"""Request-scoped runtime tool compatibility for the selected Gateway route.

Compatibility planning lives in ``tool_compatibility``. This module is a
stable import façade so existing ``from runtime_tool_compatibility import ...``
callers keep working.
"""

from tool_compatibility import (
    ADAPT,
    CUSTOM_FREEFORM,
    CUSTOM_INPUT_KEY,
    CUSTOM_OUTPUT_KEY,
    NAMESPACE,
    NATIVE,
    OMIT,
    PLAIN_FUNCTION,
    REQUIRED_BUT_UNAVAILABLE,
    SELECTED_PROVIDER_HOSTED,
    TOOL_SEARCH,
    TOOL_SEARCH_INPUT_KEY,
    TOOL_SEARCH_OUTPUT_KEY,
    UNKNOWN_FUTURE_KIND,
    AliasRecord,
    AliasRegistry,
    CompatibilityDiagnostics,
    CompatibilityStreamState,
    HostedCapabilityFacts,
    ProtocolCapabilities,
    RequestScopedToolAliasRegistry,
    RequiredToolUnavailableError,
    ToolCompatibilityEntry,
    ToolCompatibilityError,
    ToolCompatibilityPlan,
    ToolPlan,
    build_plan,
    build_tool_compatibility_plan,
    classify_declaration,
)
from tool_compatibility import __all__ as __all__
from tool_compatibility.plan import _is_opaque_collaboration_history_item
