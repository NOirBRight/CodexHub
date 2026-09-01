"""Stable constants and semantic helpers for the compatibility pipeline.

Compatibility submodules keep this module for immutable shared values. Mutable
adapter behavior is imported as an owning module and read at the call site so
tests and production both observe the same patchable seam.
"""

from __future__ import annotations

from gateway_request import (
    EMBEDDED_MODEL_RE,
    has_browser_context_signal as _has_browser_context_signal,
    reasoning_param_is_unsupported as _reasoning_param_is_unsupported,
    sanitize_official_input_reasoning_items as _sanitize_official_input_reasoning_items,
    sanitize_official_reasoning_items as _sanitize_official_reasoning_items,
    sanitize_third_party_reasoning_items as _sanitize_third_party_reasoning_items,
    strip_reasoning_encrypted_content as _strip_reasoning_encrypted_content,
)
from gateway_stream_semantics import (
    MULTI_AGENT_TOOL_NAMES,
    RESPONSES_TERMINAL_EVENT_TYPES,
    chat_content_text as _chat_content_text,
    hide_reasoning_text as _hide_reasoning_text,
    is_raw_reasoning_stream_event as _is_raw_reasoning_stream_event,
    normalize_responses_message_input_items as _normalize_responses_message_input_items,
    normalize_responses_string_input as _normalize_responses_string_input,
    sse_json_line as _sse_json_line,
    text_contains_lifecycle_final_report as _text_contains_lifecycle_final_report,
)

# Compatibility-pipeline constants owned by the host after the facade removal.


EXCESSIVE_TOOL_LOOP_BOUND = 3
EXCESSIVE_TOOL_LOOP_ERROR_CODE = "excessive_tool_loop"
NATIVE_RESPONSES_TOOL_CONTRACT_ERROR_CODE = "invalid_native_responses_tool_contract"
OLLAMA_REASONING_EFFORT_ALIASES = {"xhigh": "max"}
STRUCTURED_TOOL_PROTOCOLS = {"responses_structured", "chat_tools"}
