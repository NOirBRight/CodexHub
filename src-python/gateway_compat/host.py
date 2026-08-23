"""Host helpers for the compatibility pipeline, bound from owning modules.

Compatibility submodules read ``host.<name>`` at call time. Constants are
owned here or imported from their owning module; functions that tests patch on
owning modules are forwarded so those patches stay live.
"""

from __future__ import annotations

from typing import Any

from gateway_request import (
    EMBEDDED_MODEL_RE,
    _has_browser_context_signal,
    _reasoning_param_is_unsupported,
    _sanitize_official_input_reasoning_items,
    _sanitize_official_reasoning_items,
    _strip_reasoning_encrypted_content,
)
from gateway_stream_semantics import (
    MULTI_AGENT_TOOL_NAMES,
    RESPONSES_TERMINAL_EVENT_TYPES,
    _chat_content_text,
    _hide_reasoning_text,
    _is_raw_reasoning_stream_event,
    _normalize_responses_message_input_items,
    _normalize_responses_string_input,
    _sse_json_line,
    _text_contains_lifecycle_final_report,
)

# Compatibility-pipeline constants owned by the host after the facade removal.
EXCESSIVE_TOOL_LOOP_BOUND = 3
EXCESSIVE_TOOL_LOOP_ERROR_CODE = "excessive_tool_loop"
NATIVE_RESPONSES_TOOL_CONTRACT_ERROR_CODE = "invalid_native_responses_tool_contract"
OLLAMA_REASONING_EFFORT_ALIASES = {"xhigh": "max"}
STRUCTURED_TOOL_PROTOCOLS = {"responses_structured", "chat_tools"}


def _apply_patch_adapter() -> Any:
    import apply_patch_adapter as _module

    return _module.apply_patch_adapter()


def _collaboration_adapter() -> Any:
    import collaboration_adapter as _module

    return _module.collaboration_adapter()


def _tool_surface_adapter() -> Any:
    import tool_surface_adapter as _module

    return _module.tool_surface_adapter()


def _catalog_output_limit(model_id: str) -> tuple[int | None, bool]:
    import gateway_catalog_runtime

    return gateway_catalog_runtime.catalog_output_limit(model_id)


def _collaboration_context_with_protocol(
    event_context: Any,
    protocol: str | None,
) -> Any:
    return _collaboration_adapter().context_with_protocol(event_context, protocol)


def _is_collaboration_v2_context(event_context: Any) -> bool:
    return _collaboration_adapter().is_v2_context(event_context)


def _resolve_collaboration_boundary(
    payload: Any,
    event_context: Any,
    *,
    surface: str = "request",
) -> str | None:
    return _collaboration_adapter().resolve_boundary(
        payload,
        event_context,
        surface=surface,
    )


def _write_adapter_event(event_context: Any, event: str, **fields: Any) -> None:
    import gateway_events

    gateway_events.write_adapter_event(event_context, event, **fields)


def write_proxy_event(event: str, **fields: Any) -> None:
    import gateway_events

    gateway_events.write_proxy_event(event, **fields)
