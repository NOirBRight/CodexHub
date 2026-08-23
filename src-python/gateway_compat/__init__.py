"""Compatibility application pipeline for third-party and official Gateway routes.

Planning lives in ``tool_compatibility``; this package applies a request plan to
bodies and SSE frames. Host helpers that still live in ``gateway_runtime`` are
injected via ``host.bind`` at facade load time.
"""

from __future__ import annotations

import importlib
import sys

from . import host as host

_SUBMODULES = (
    "request",
    "response",
    "sse",
    "multi_agent",
    "official_passthrough",
)
_SUBMODULE_NAMES = frozenset(_SUBMODULES) | {"host", "api"}
_MISSING = object()


def _load_submodule(name: str):
    full = f"{__name__}.{name}"
    loaded = sys.modules.get(full)
    if loaded is not None:
        return loaded
    return importlib.import_module(f".{name}", __name__)


def compatible_request_body(*args, **kwargs):
    return _load_submodule("request").compatible_request_body(*args, **kwargs)


def compatible_response_body(*args, **kwargs):
    return _load_submodule("response").compatible_response_body(*args, **kwargs)


def compatible_sse_line(*args, **kwargs):
    return _load_submodule("sse").compatible_sse_line(*args, **kwargs)


def official_passthrough_request_body(*args, **kwargs):
    return _load_submodule("official_passthrough").official_passthrough_request_body(*args, **kwargs)


def transparent_request_body(*args, **kwargs):
    return _load_submodule("official_passthrough").transparent_request_body(*args, **kwargs)


def lookup(name: str):
    """Return a live attribute from the owning submodule."""

    for mod_name in _SUBMODULES:
        mod = sys.modules.get(f"{__name__}.{mod_name}")
        if mod is None:
            continue
        value = vars(mod).get(name, _MISSING)
        if value is not _MISSING:
            return value
    for mod_name in _SUBMODULES:
        mod = _load_submodule(mod_name)
        value = vars(mod).get(name, _MISSING)
        if value is not _MISSING:
            return value
    raise AttributeError(name)


def __getattr__(name: str):
    if name in _SUBMODULE_NAMES:
        return _load_submodule(name)
    return lookup(name)


__all__ = [
    "compatible_request_body",
    "compatible_response_body",
    "compatible_sse_line",
    "official_passthrough_request_body",
    "transparent_request_body",
]
