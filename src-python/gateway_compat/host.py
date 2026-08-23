"""Runtime helpers injected by the Gateway facade during T3.

These names still live in ``gateway_runtime`` / adapters. ``bind`` copies the
live objects so compatibility code does not import the exec facade.
"""

from __future__ import annotations

from typing import Any

EMBEDDED_MODEL_RE = None
EXCESSIVE_TOOL_LOOP_BOUND = None
EXCESSIVE_TOOL_LOOP_ERROR_CODE = None
MULTI_AGENT_TOOL_NAMES = None
NATIVE_RESPONSES_TOOL_CONTRACT_ERROR_CODE = None
OLLAMA_REASONING_EFFORT_ALIASES = None
RESPONSES_TERMINAL_EVENT_TYPES = None
STRUCTURED_TOOL_PROTOCOLS = None
WORKER_BINDING_SIGNING_ROOT = None
_apply_patch_adapter = None
_catalog_output_limit = None
_chat_content_text = None
_collaboration_adapter = None
_collaboration_context_with_protocol = None
_has_browser_context_signal = None
_hide_reasoning_text = None
_is_collaboration_v2_context = None
_is_raw_reasoning_stream_event = None
_normalize_responses_message_input_items = None
_normalize_responses_string_input = None
_reasoning_param_is_unsupported = None
_resolve_collaboration_boundary = None
_sanitize_official_input_reasoning_items = None
_sanitize_official_reasoning_items = None
_sse_json_line = None
_strip_reasoning_encrypted_content = None
_text_contains_lifecycle_final_report = None
_tool_surface_adapter = None
_write_adapter_event = None
write_proxy_event = None


def bind(namespace: dict[str, Any]) -> None:
    globals().update({key: namespace[key] for key in list(globals()) if key in namespace})
