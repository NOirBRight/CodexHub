"""Compatibility application pipeline for third-party and official Gateway routes.

Planning lives in ``tool_compatibility``; this package applies a request plan to
bodies and SSE frames. Host helpers are imported from their owning modules in
``host``.
"""

from __future__ import annotations

import route_plan as route_plan
import collaboration_adapter as _collaboration_adapter

from . import host as host
from . import multi_agent as multi_agent
from . import official_passthrough as official_passthrough
from . import request as request
from . import response as response
from . import sse as sse


def compatible_request_body(*args, **kwargs):
    return request.compatible_request_body(*args, **kwargs)


def compatible_response_body(*args, **kwargs):
    return response.compatible_response_body(*args, **kwargs)


def compatible_sse_line(*args, **kwargs):
    return sse.compatible_sse_line(*args, **kwargs)


def official_passthrough_request_body(*args, **kwargs):
    return official_passthrough.official_passthrough_request_body(*args, **kwargs)


def transparent_request_body(*args, **kwargs):
    return official_passthrough.transparent_request_body(*args, **kwargs)


def adapt_third_party_apply_patch_response_body(*args, **kwargs):
    return response._adapt_third_party_apply_patch_response_body(*args, **kwargs)


def external_tool_protocol(*args, **kwargs):
    return route_plan._external_tool_protocol(*args, **kwargs)


def normalize_transparent_tool_schema_booleans(*args, **kwargs):
    return official_passthrough._normalize_transparent_tool_schema_booleans(*args, **kwargs)


def resolve_collaboration_boundary(*args, **kwargs):
    return _collaboration_adapter.resolve_boundary(*args, **kwargs)


def rewrite_structured_tool_input_items(*args, **kwargs):
    return official_passthrough._rewrite_structured_tool_input_items(*args, **kwargs)


__all__ = [
    "adapt_third_party_apply_patch_response_body",
    "compatible_request_body",
    "compatible_response_body",
    "compatible_sse_line",
    "external_tool_protocol",
    "host",
    "multi_agent",
    "normalize_transparent_tool_schema_booleans",
    "official_passthrough",
    "official_passthrough_request_body",
    "request",
    "resolve_collaboration_boundary",
    "response",
    "rewrite_structured_tool_input_items",
    "sse",
    "transparent_request_body",
]
