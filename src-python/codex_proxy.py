from __future__ import annotations

import argparse
from copy import deepcopy
from collections.abc import Iterable as IterableABC
from dataclasses import dataclass, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from enum import Enum
import gzip
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import queue
import re
import socket
import sqlite3
import ssl
from functools import partial
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import tomllib
from typing import Any, Callable, Mapping, NoReturn
import uuid
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen

try:
    from urllib.request import getproxies_registry
except ImportError:  # pragma: no cover - Windows-only urllib helper.
    getproxies_registry = None

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
VENDORED_URLLIB3_WHEEL = VENDOR_DIR / "urllib3-2.7.0-py3-none-any.whl"
if not VENDORED_URLLIB3_WHEEL.is_file():
    raise RuntimeError(f"missing pinned Gateway transport dependency: {VENDORED_URLLIB3_WHEEL}")
sys.path.insert(0, str(VENDORED_URLLIB3_WHEEL))

import urllib3

from sse_events import (
    DEFAULT_MAX_FRAME_BYTES,
    SseAssemblerClosedError,
    SseEvent,
    SseEventAssembler,
    SseFrameTooLargeError,
)
from protocol_translation import (
    ChatToResponsesStreamConverter,
    ResponsesToChatStreamConverter,
    UnsupportedProtocolTranslationError,
    UpstreamStreamIncompleteError,
    chat_completion_body_to_stream_chunks,
    chat_completion_error_body,
    chat_completion_to_response_body,
    chat_completions_request_to_responses_body,
    chat_content_to_responses_content,
    chat_messages_to_responses_input,
    chat_stream_chunks_to_response_events,
    chat_tool_choice_to_responses_tool_choice,
    chat_tools_to_responses_tools,
    events_to_responses_body,
    response_body_to_chat_completion_body,
    response_body_to_response_sse_events,
    response_events_to_chat_stream_chunks,
    responses_content_to_chat_content,
    responses_input_to_chat_messages,
    responses_request_to_chat_completion_body,
    responses_tool_choice_to_chat_tool_choice,
    responses_tools_to_chat_tools,
)
from runtime_tool_compatibility import (
    ProtocolCapabilities as RuntimeProtocolCapabilities,
    ToolCompatibilityError as RuntimeToolCompatibilityError,
    ToolCompatibilityPlan as RuntimeToolCompatibilityPlan,
    build_tool_compatibility_plan,
)

from codex_semantic_adapter import (
    BINDING_ACCEPTED as _BINDING_ACCEPTED,
    COLLABORATION_V1 as _COLLABORATION_V1,
    COLLABORATION_V2 as _COLLABORATION_V2,
    CollaborationBoundaryError as _CollaborationBoundaryError,
    classify_collaboration_payload as _classify_collaboration_payload,
    collaboration_protocols as _collaboration_protocols,
    coerce_number as _semantic_coerce_number,
    coerce_target as _semantic_coerce_target,
    coerce_targets as _semantic_coerce_targets,
    multi_agent_discovery_arguments as _semantic_multi_agent_discovery_arguments,
    normalize_multi_agent_arguments as _semantic_normalize_multi_agent_arguments,
    normalize_tool_search_arguments as _semantic_normalize_tool_search_arguments,
    strict_json_object as _semantic_strict_json_object,
    validate_effective_worker_binding as _semantic_validate_effective_worker_binding,
    validate_requested_worker_binding as _semantic_validate_requested_worker_binding,
    validate_worker_selector as _semantic_validate_worker_selector,
)

from catalog import (
    CatalogVisibility,
    CatalogPolicy,
    canonical_model_id,
    deny_match_model_id,
    is_internal_model,
    load_catalog_models,
    load_policy,
    model_visibility,
    should_include_external_provider_model,
    should_include_model,
)
from catalog_sync import (
    CONTEXT_WINDOW_OUTPUT_FALLBACK_SOURCE,
    GENERATED_CATALOG_PATH,
    LEGACY_GENERATED_CATALOG_PATH,
    POLICY_PATH,
    existing_generated_catalog_path,
    known_official_model_ids as catalog_known_official_model_ids,
    official_short_display_name,
)
from model_limits import (
    CURRENT_DIRECT_OFFICIAL_SOURCE,
    DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE,
)
from codex_auth import CodexAuthError, access_token as codex_access_token, account_id as codex_account_id
from providers_config import resolve_external_model_alias, resolve_ollama_cloud_model
from subagent_policy import (
    deterministic_required_action,
    guidance_enabled as _subagent_policy_guidance_enabled,
    semantic_repair_enabled as _subagent_policy_semantic_repair_enabled,
    subagent_assist_mode as _subagent_policy_assist_mode,
)
from subagent_dynamic_dag import build_dynamic_dag_workflow, dynamic_dag_guidance_message, is_dynamic_dag_request
from subagent_scheduler import bounded_workflow_from_exact_prompts, compute_allowed_actions, workflow_complete
from subagent_state import build_subagent_state, is_worker_subagent_request, state_guidance_message
from websocket_transport import (
    WebSocketProtocolError,
    close_frame,
    read_frame,
    redacted_handshake_metadata,
    websocket_upgrade_response_headers,
    write_frame,
)
import proxy_telemetry
import bounded_event_writer
import diagnostic_recorder
import worker_binding_signing

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency on older Python installs.
    zstandard = None

DECODE_ERRORS = (OSError, zlib.error) + ((zstandard.ZstdError,) if zstandard is not None else ())

OFFICIAL_POOL_MAX_CONNECTIONS = 16
OFFICIAL_POOL_MAX_IDLE_SECONDS = 30.0
OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS = 300.0
OFFICIAL_CONNECT_TIMEOUT_SECONDS = 15.0
OFFICIAL_PASSTHROUGH_FIRST_EVENT_ATTEMPTS = 2
OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS = 1.0
OFFICIAL_TCP_KEEPALIVE_IDLE_MS = 5000
OFFICIAL_TCP_KEEPALIVE_INTERVAL_MS = 5000
OFFICIAL_HTTP_POOLS: dict[str, Any] = {}
OFFICIAL_HTTP_POOLS_LOCK = threading.Lock()
_OFFICIAL_ATTEMPT_CONNECTION_STATE = threading.local()


def _reset_official_attempt_connection_disposition() -> None:
    """Clear the per-thread lease label before a new Official pool request."""

    _OFFICIAL_ATTEMPT_CONNECTION_STATE.disposition = "unobserved"


def _set_official_attempt_connection_disposition(disposition: str) -> None:
    if disposition in {"new", "reused"}:
        _OFFICIAL_ATTEMPT_CONNECTION_STATE.disposition = disposition


def _official_attempt_connection_disposition() -> str:
    disposition = getattr(_OFFICIAL_ATTEMPT_CONNECTION_STATE, "disposition", "unobserved")
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _clear_official_attempt_connection_disposition() -> None:
    try:
        del _OFFICIAL_ATTEMPT_CONNECTION_STATE.disposition
    except AttributeError:
        pass


def _official_socket_options() -> list[tuple[int, int, int]]:
    options = list(urllib3.connection.HTTPConnection.default_socket_options)
    options.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
    if not sys.platform.startswith("win"):
        if hasattr(socket, "TCP_KEEPIDLE"):
            options.append(
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, max(1, OFFICIAL_TCP_KEEPALIVE_IDLE_MS // 1000))
            )
        if hasattr(socket, "TCP_KEEPINTVL"):
            options.append(
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, max(1, OFFICIAL_TCP_KEEPALIVE_INTERVAL_MS // 1000))
            )
        if hasattr(socket, "TCP_KEEPCNT"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3))
    return options


def _configure_official_windows_keepalive(sock: Any) -> None:
    if sys.platform.startswith("win") and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        sock.ioctl(
            socket.SIO_KEEPALIVE_VALS,
            (1, OFFICIAL_TCP_KEEPALIVE_IDLE_MS, OFFICIAL_TCP_KEEPALIVE_INTERVAL_MS),
        )


class _OfficialHTTPSConnection(urllib3.connection.HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        if self.sock is not None:
            _configure_official_windows_keepalive(self.sock)


class _OfficialHTTPSConnectionPool(urllib3.connectionpool.HTTPSConnectionPool):
    ConnectionCls = _OfficialHTTPSConnection

    def _get_conn(self, timeout: float | None = None) -> Any:
        connection = super()._get_conn(timeout)
        released_at = getattr(connection, "_codexhub_released_at", None)
        try:
            disposition = (
                "reused" if isinstance(released_at, (int, float)) and getattr(connection, "sock", None) else "new"
            )
        except Exception:
            disposition = "unobserved"
        idle_seconds = time.monotonic() - released_at if isinstance(released_at, (int, float)) else None
        max_idle_seconds = (
            OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS if self.proxy is not None else OFFICIAL_POOL_MAX_IDLE_SECONDS
        )
        if idle_seconds is not None and idle_seconds >= max_idle_seconds:
            connection.close()
            disposition = "new"
        try:
            connection._codexhub_diagnostic_connection_disposition = disposition
        except Exception:
            pass
        _set_official_attempt_connection_disposition(disposition)
        return connection

    def _put_conn(self, connection: Any) -> None:
        if connection is not None:
            connection._codexhub_released_at = time.monotonic()
        super()._put_conn(connection)


class _OfficialPooledResponse:
    def __init__(self, response: Any):
        self._response = response
        self._exhausted = False
        self._released = False
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers
        self.connection_disposition = _connection_disposition(getattr(response, "connection", None))
        self._terminal_drain_socket: Any = None
        self._terminal_drain_original_timeout: float | None = None

    def read(self, amount: int | None = None) -> bytes:
        try:
            data = self._response.read(amount)
        except urllib3.exceptions.HTTPError as exc:
            translated = _stdlib_transport_error(exc)
            raise translated from exc
        if amount is None or data == b"":
            self._exhausted = True
        return data

    def readline(self, limit: int = -1) -> bytes:
        try:
            data = self._response.readline(limit)
        except urllib3.exceptions.HTTPError as exc:
            translated = _stdlib_transport_error(exc)
            raise translated from exc
        if data == b"":
            self._exhausted = True
        return data

    def getcode(self) -> int:
        return self.status

    def shorten_terminal_drain_timeout(self, timeout_seconds: float) -> None:
        connection = getattr(self._response, "connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None or self._terminal_drain_socket is not None:
            return
        try:
            original_timeout = sock.gettimeout()
            sock.settimeout(timeout_seconds)
        except OSError:
            return
        self._terminal_drain_socket = sock
        self._terminal_drain_original_timeout = original_timeout

    def _restore_terminal_drain_timeout(self) -> None:
        if self._terminal_drain_socket is None:
            return
        try:
            self._terminal_drain_socket.settimeout(self._terminal_drain_original_timeout)
        except OSError:
            pass
        self._terminal_drain_socket = None
        self._terminal_drain_original_timeout = None

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        if self._exhausted:
            self._restore_terminal_drain_timeout()
            self._response.release_conn()
        else:
            self._response.close()
            self._response.release_conn()

    def __enter__(self) -> "_OfficialPooledResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.close()
        return False


def _connection_disposition(connection: Any) -> str:
    try:
        disposition = getattr(connection, "_codexhub_diagnostic_connection_disposition", "unobserved")
    except Exception:
        return "unobserved"
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _official_proxy_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname:
        try:
            if proxy_bypass(parsed.hostname):
                return None
        except OSError:
            pass
    proxies = getproxies()
    proxy = proxies.get(parsed.scheme)
    if (
        not proxy
        and sys.platform.startswith("win")
        and callable(getproxies_registry)
        and not any(proxies.get(scheme) for scheme in ("http", "https"))
    ):
        try:
            proxy = getproxies_registry().get(parsed.scheme)
        except OSError:
            proxy = None
    return str(proxy) if proxy else None


def _official_pool_manager(url: str) -> Any:
    proxy_url = _official_proxy_url(url)
    pool_key = proxy_url or "direct"
    existing = OFFICIAL_HTTP_POOLS.get(pool_key)
    if existing is not None:
        return existing
    with OFFICIAL_HTTP_POOLS_LOCK:
        existing = OFFICIAL_HTTP_POOLS.get(pool_key)
        if existing is None:
            pool_options = {
                "num_pools": 4,
                "maxsize": OFFICIAL_POOL_MAX_CONNECTIONS,
                "block": True,
                "retries": False,
                "socket_options": _official_socket_options(),
            }
            existing = (
                urllib3.ProxyManager(proxy_url, **pool_options)
                if proxy_url is not None
                else urllib3.PoolManager(**pool_options)
            )
            existing.pool_classes_by_scheme = {
                **existing.pool_classes_by_scheme,
                "https": _OfficialHTTPSConnectionPool,
            }
            OFFICIAL_HTTP_POOLS[pool_key] = existing
        return existing


def _stdlib_transport_error(exc: BaseException) -> BaseException:
    pending: list[Any] = [exc]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop(0)
        if not isinstance(candidate, BaseException) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if isinstance(candidate, (ssl.SSLError, TimeoutError, ConnectionError, OSError, IncompleteRead)):
            return candidate
        pending.extend(
            value
            for value in (
                getattr(candidate, "reason", None),
                candidate.__cause__,
                candidate.__context__,
                *candidate.args,
            )
            if isinstance(value, BaseException)
        )
    if isinstance(exc, urllib3.exceptions.TimeoutError):
        return TimeoutError(str(exc))
    return URLError(exc)


def _official_urlopen(request: Request, *, timeout: float) -> Any:
    manager = _official_pool_manager(request.full_url)
    headers = {key: value for key, value in request.header_items() if key.lower() != "connection"}
    _reset_official_attempt_connection_disposition()
    try:
        response = manager.request(
            request.get_method(),
            request.full_url,
            body=request.data,
            headers=headers,
            preload_content=False,
            decode_content=False,
            redirect=False,
            retries=False,
            timeout=urllib3.Timeout(connect=min(timeout, OFFICIAL_CONNECT_TIMEOUT_SECONDS), read=timeout),
            pool_timeout=timeout,
        )
    except urllib3.exceptions.HTTPError as exc:
        translated = _stdlib_transport_error(exc)
        disposition = _official_attempt_connection_disposition()
        if disposition != "unobserved":
            try:
                setattr(translated, "_codexhub_diagnostic_connection_disposition", disposition)
            except Exception:
                pass
        raise translated from exc
    finally:
        _clear_official_attempt_connection_disposition()

    pooled_response = _OfficialPooledResponse(response)
    if response.status >= 400:
        raise HTTPError(
            request.full_url,
            response.status,
            str(response.reason or "upstream error"),
            response.headers,
            pooled_response,
        )
    return pooled_response


OFFICIAL_BASE_URL = "https://api.openai.com/v1"
OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
PROXY_BUILD = "2026-07-04-browser-tool-exposure"
PROXY_FEATURES = [
    "compressed-request-routing",
    "provider-alias-routing",
    "local-responses-probe-fast-reject",
    "internal-history-item-normalization",
    "external-reasoning-hidden",
    "tool-name-guard",
    "third-party-subagent-tool-alias",
    "third-party-tool-search-call-shim",
    "third-party-multi-agent-discovery-shim",
    "third-party-multi-agent-namespace-shim",
    "third-party-multi-agent-wait-close-argument-shim",
    "third-party-explicit-codex-native-tools",
    "third-party-json-schema-type-array-guard",
    "third-party-multi-agent-discovery-fallback",
    "third-party-native-tools-stay-visible",
    "third-party-multi-agent-discovery-guidance",
    "third-party-tool-search-disabled",
    "third-party-spawn-hidden-while-agent-open",
    "third-party-multi-agent-status-guidance",
    "third-party-unsupported-reasoning-strip",
    "third-party-subagent-observability",
    "official-invalid-tool-assistant-shim",
    "upstream-incomplete-read-guard",
    "chat-completions-gateway",
    "third-party-open-agent-id-schema-guidance",
    "third-party-ordered-agent-lifecycle-guidance",
    "third-party-single-loop-completion-gate",
    "ollama-output-token-cap",
    "official-upstream-open-retry",
    "compact-text-only-tool-strip",
    "compact-empty-response-guard",
    "compact-empty-response-retry",
    "stream-read-error-retry-before-downstream",
    "downstream-sse-keepalive",
    "split-transport-model-event-sse-idle-timeouts",
    "capacity-aware-upstream-retry",
    "stream-transient-global-retry-budget",
    "third-party-tool-terminal-synthesis",
    "browser-context-skill-guidance",
    "third-party-multi-agent-deterministic-repair",
    "third-party-required-subagent-action-repair",
    "third-party-chat-output-repair-parity",
    "official-upstream-connection-pool",
    "official-upstream-idle-connection-expiry",
    "official-terminal-sse-authoritative",
    "official-title-responses-lite-header-strip",
    "zstd-request-body-runtime",
    "raw-provider-probe-opt-out",
]
DEFAULT_OFFICIAL_PREFIXES = ("gpt-",)
OFFICIAL_ALIAS_PREFIX = "openai/"
OFFICIAL_ULTRA_REASONING_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra"}
OFFICIAL_RESPONSES_LITE_UNSUPPORTED_MODELS = {"gpt-5.4", "gpt-5.4-mini"}
OFFICIAL_FAST_VARIANT_SERVICE_TIER = "priority"
OFFICIAL_FAST_VARIANT_BASE_MODELS = {
    "gpt-5.5-fast": "gpt-5.5",
    "gpt-5.4-fast": "gpt-5.4",
}
OFFICIAL_FAST_VARIANT_DISPLAY_NAMES = {
    "gpt-5.5-fast": "5.5 Fast",
    "gpt-5.4-fast": "5.4 Fast",
}
OLLAMA_REASONING_EFFORT_ALIASES = {"xhigh": "max"}
UNSUPPORTED_REASONING_MODEL_PREFIXES = ("kimi-k2.6", "kimi-k2.7")
UPSTREAM_MAX_OUTPUT_TOKEN_CAPS = {
    "minimax-m3": 131072,
}
OFFICIAL_ENCRYPTED_CONTENT_PREFIX = "gAAAA"
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MULTI_AGENT_TOOL_NAMES = {
    "spawn_agent",
    "wait_agent",
    "close_agent",
    "resume_agent",
    "send_input",
}
MULTI_AGENT_NAMESPACE_ALIASES = {
    "multi_agent_v1",
    "mcp__multi_agent_v1",
}
NODE_REPL_NAMESPACE = "mcp__node_repl"
THIRD_PARTY_TOOL_NAME_ALIASES = {
    f"multi_agent_v1__{tool_name}": tool_name for tool_name in MULTI_AGENT_TOOL_NAMES
}
THIRD_PARTY_TOOL_NAME_ALIASES.update(
    {f"multi_agent_v1.{tool_name}": tool_name for tool_name in MULTI_AGENT_TOOL_NAMES}
)
THIRD_PARTY_TOOL_NAME_ALIASES.update(
    {f"multi_agent_v1{tool_name}": tool_name for tool_name in MULTI_AGENT_TOOL_NAMES}
)
THIRD_PARTY_TOOL_NAME_ALIASES.update(
    {f"mcp__multi_agent_v1__{tool_name}": tool_name for tool_name in MULTI_AGENT_TOOL_NAMES}
)
THIRD_PARTY_TOOL_NAME_ALIASES.update(
    {f"mcp__multi_agent_v1.{tool_name}": tool_name for tool_name in MULTI_AGENT_TOOL_NAMES}
)
MULTI_AGENT_DISCOVERY_QUERY = "spawn_agent multi_agent subagent native Codex"
WORKER_SELECTOR_ERROR_CODE = "external_worker_selector_rejected"
WORKER_BINDING_ERROR_CODE = "external_worker_binding_rejected"
WORKER_REQUESTED_BINDING_FIELD = "_codexhub_worker_requested_binding"
WORKER_REQUESTED_BINDING_VERSION = "codexhub.requested-worker-binding.v1"
WORKER_REQUESTED_BINDING_FIELDS = {
    "contract_version",
    "agent_type",
    "model",
    "reasoning",
    "signature",
}
MULTI_AGENT_DISCOVERY_TOOLS = [
    {
        "type": "namespace",
        "name": "multi_agent_v1",
        "description": "Tools for spawning and managing Codex sub-agents.",
        "tools": [
            {
                "type": "function",
                "name": "spawn_agent",
                "description": "Spawn a sub-agent. Use namespace multi_agent_v1 and function name spawn_agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent_type": {"type": "string", "enum": ["worker", "general"]},
                        "fork_context": {"type": "boolean"},
                        "message": {"type": "string"},
                    },
                    "required": ["agent_type"],
                    "additionalProperties": True,
                },
            },
            {
                "type": "function",
                "name": "wait_agent",
                "description": "Wait for one or more spawned sub-agents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "targets": {"type": "array", "items": {"type": "string"}},
                        "timeout_ms": {"type": "number"},
                    },
                    "required": ["targets"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "close_agent",
                "description": "Close a spawned sub-agent when it is no longer needed.",
                "parameters": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "resume_agent",
                "description": "Resume a previously closed sub-agent by id.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "send_input",
                "description": "Send a message to an existing sub-agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "message": {"type": "string"},
                        "interrupt": {"type": "boolean"},
                    },
                    "required": ["target"],
                    "additionalProperties": True,
                },
            },
        ],
    }
]
TOOL_PROTOCOLS = {"auto", "responses_structured", "chat_tools", "text_compat", "none"}
STRUCTURED_TOOL_PROTOCOLS = {"responses_structured", "chat_tools"}
TOOL_SURFACE_STRATEGIES = {"eager", "deferred_core"}
TOOL_SURFACE_STRATEGY_ERROR_CODE = "invalid_external_tool_surface_strategy"
NATIVE_RESPONSES_TOOL_CODECS = {"none", "strict_apply_patch"}
NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE = "invalid_native_responses_tool_codec"
NATIVE_RESPONSES_TOOL_CONTRACT_ERROR_CODE = "invalid_native_responses_tool_contract"
COLLABORATION_BOUNDARY_ERROR_CODE = "invalid_collaboration_boundary"
TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL = {
    "type": "function",
    "name": "tool_search",
    "description": "Discover deferred Codex tools by keyword. Use this before calling a tool that is not already visible.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
TOOL_SEARCH_EMPTY_MISS_BOUND = 2
TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION = "identical_exact_query"
TOOL_SEARCH_UNAVAILABLE_STATUS = "unavailable"
EXCESSIVE_TOOL_LOOP_BOUND = 3
EXCESSIVE_TOOL_LOOP_ERROR_CODE = "excessive_tool_loop"
BROWSER_CONTEXT_MARKERS = (
    "# in app browser",
    "# browser comments",
    "browser visual feedback",
)
BROWSER_CURRENT_URL_RE = re.compile(
    r"(?im)^\s*(?:current\s+url|current\s+browser\s+url|browser\s+url|url)\s*:\s*https?://\S+"
)
BROWSER_CONTEXT_GUIDANCE_SENTINEL = "Codex browser context detected."
BROWSER_CONTEXT_GUIDANCE = (
    BROWSER_CONTEXT_GUIDANCE_SENTINEL
    + "\nRequired browser-control workflow:\n"
    "- Load and follow the browser:control-in-app-browser skill before saying browser control is unavailable.\n"
    '- For OpenAI/Codex native discovery, use tool_search with query "node_repl js" if mcp__node_repl.js is not already visible.\n'
    "- Browser control is unavailable only when that search does not return mcp__node_repl.js, or when mcp__node_repl.js reports no in-app browser session.\n"
    "- If executable alias mcp__node_repl__js is visible, use it directly to bootstrap browser-client.mjs and select the iab browser.\n"
    '- In a CLI/no-browser environment, report "browser session unavailable"; do not report "browser tool not exposed".'
)
INTERNAL_INPUT_ITEM_TYPES = {
    "compaction",
    "compaction_trigger",
    "reasoning",
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "web_search_call",
    "tool_search_call",
    "tool_search_output",
}
EMBEDDED_MODEL_RE = re.compile(rb'"model"\s*:\s*"(?:[^"\\]|\\.)+"')
FORM_MODEL_RE = re.compile(rb'name="model"(?:\r?\n[^\r\n]*)*\r?\n\r?\n([^\r\n]+)')

HOP_BY_HOP_REQUEST_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "server",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

PROXY_DIR = Path(__file__).resolve().parent
def _env_positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default)) or str(default)))
    except ValueError:
        return default


def _runtime_codex_dir() -> Path:
    codex_home_env = os.environ.get("CODEX_HOME")
    if codex_home_env:
        return Path(codex_home_env)
    return Path.home() / ".codex"


RUNTIME_CODEX_DIR = _runtime_codex_dir()
RUNTIME_PROXY_DIR = RUNTIME_CODEX_DIR / "proxy"
WORKER_BINDING_SIGNING_ROOT = RUNTIME_PROXY_DIR
OFFICIAL_REFRESH_STATE_FILENAME = "official-refresh-state.json"
PROXY_EVENT_LOG_PATH = RUNTIME_PROXY_DIR / "codex-proxy-events.jsonl"
PROXY_TEXT_LOG_PATH = RUNTIME_PROXY_DIR / "codex-proxy.log"
# Both limits apply to serialized Gateway telemetry still awaiting a sink write,
# including an in-flight batch, so slow storage cannot grow request memory.
GATEWAY_EVENT_QUEUE_MAX_RECORDS = _env_positive_int("CODEX_GATEWAY_EVENT_QUEUE_MAX_RECORDS", 4096)
GATEWAY_EVENT_QUEUE_MAX_BYTES = _env_positive_int("CODEX_GATEWAY_EVENT_QUEUE_MAX_BYTES", 4 * 1024 * 1024)
GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS = 5.0
GATEWAY_USER_REQUESTED_SHUTDOWN_BUDGET_SECONDS = 2.0
USER_REQUESTED_SHUTDOWN_OUTCOME = "user_requested_shutdown"


class GatewayUserRequestedShutdown(RuntimeError):
    """Raised in a request worker after the local Gateway begins retirement."""


class GatewayRequestAdmission:
    """One admitted Gateway request and the upstream transport it may cancel."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._upstream_transport: Any | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def attach_upstream_transport(self, transport: Any) -> None:
        with self._lock:
            self._upstream_transport = transport
            cancelled = self._cancelled.is_set()
        if cancelled:
            self._close_upstream_transport(transport)

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            transport = self._upstream_transport
        if transport is not None:
            self._close_upstream_transport(transport)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise GatewayUserRequestedShutdown(USER_REQUESTED_SHUTDOWN_OUTCOME)

    def wait_for_cancellation(self, timeout: float) -> bool:
        return self._cancelled.wait(timeout=max(0.0, timeout))

    @staticmethod
    def _close_upstream_transport(transport: Any) -> None:
        close = getattr(transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class GatewayShutdownController:
    """Authenticated local control-plane state for Gateway retirement."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        shutdown_budget_seconds: float = GATEWAY_USER_REQUESTED_SHUTDOWN_BUDGET_SECONDS,
    ) -> None:
        self._clock = clock
        self._shutdown_budget_seconds = max(0.0, shutdown_budget_seconds)
        self._lock = threading.Lock()
        self._admission_open = True
        self._shutdown_started_at: float | None = None
        self._active: set[GatewayRequestAdmission] = set()
        self._active_drained = threading.Event()
        self._active_drained.set()

    def admit(self) -> GatewayRequestAdmission | None:
        with self._lock:
            if not self._admission_open:
                return None
            admission = GatewayRequestAdmission()
            self._active.add(admission)
            self._active_drained.clear()
            return admission

    def complete(self, admission: GatewayRequestAdmission) -> None:
        with self._lock:
            self._active.discard(admission)
            if not self._active:
                self._active_drained.set()

    def close_admission(self) -> int:
        with self._lock:
            self._admission_open = False
            if self._shutdown_started_at is None:
                self._shutdown_started_at = self._clock()
            active = tuple(self._active)
        for admission in active:
            admission.cancel()
        return len(active)

    def remaining_shutdown_budget_seconds(self) -> float:
        with self._lock:
            started_at = self._shutdown_started_at
        if started_at is None:
            return self._shutdown_budget_seconds
        return max(0.0, self._shutdown_budget_seconds - (self._clock() - started_at))

    @property
    def shutdown_requested(self) -> bool:
        with self._lock:
            return self._shutdown_started_at is not None

    def wait_for_active_requests(self) -> bool:
        return self._active_drained.wait(timeout=self.remaining_shutdown_budget_seconds())


GATEWAY_SHUTDOWN_CONTROLLER = GatewayShutdownController()
_GATEWAY_REQUEST_ADMISSION = threading.local()


def _gateway_shutdown_controller_for_handler(handler: Any) -> GatewayShutdownController:
    server = getattr(handler, "server", None)
    controller = getattr(server, "gateway_shutdown_controller", None)
    return controller if isinstance(controller, GatewayShutdownController) else GATEWAY_SHUTDOWN_CONTROLLER


def _activate_gateway_request(admission: GatewayRequestAdmission) -> GatewayRequestAdmission | None:
    previous = getattr(_GATEWAY_REQUEST_ADMISSION, "current", None)
    _GATEWAY_REQUEST_ADMISSION.current = admission
    return previous if isinstance(previous, GatewayRequestAdmission) else None


def _restore_gateway_request(previous: GatewayRequestAdmission | None) -> None:
    if previous is None:
        try:
            del _GATEWAY_REQUEST_ADMISSION.current
        except AttributeError:
            pass
        return
    _GATEWAY_REQUEST_ADMISSION.current = previous


def _active_gateway_request() -> GatewayRequestAdmission | None:
    current = getattr(_GATEWAY_REQUEST_ADMISSION, "current", None)
    return current if isinstance(current, GatewayRequestAdmission) else None


def _sleep_for_retry_with_gateway_cancellation(delay_seconds: float) -> None:
    admission = _active_gateway_request()
    if admission is None:
        time.sleep(delay_seconds)
        return
    if admission.wait_for_cancellation(delay_seconds):
        admission.raise_if_cancelled()


def user_requested_shutdown_payload(inbound_format: str) -> dict[str, Any]:
    message = "Gateway stopped because the user requested shutdown."
    codexhub_error = _codexhub_error_payload(
        source="gateway",
        message=message,
        status=503,
        error=USER_REQUESTED_SHUTDOWN_OUTCOME,
        error_type=USER_REQUESTED_SHUTDOWN_OUTCOME,
        failure_class=RETRY_FAILURE_PERMANENT,
    )
    if inbound_format == "chat_completions":
        return {
            "error": {
                "message": message,
                "type": USER_REQUESTED_SHUTDOWN_OUTCOME,
                "code": USER_REQUESTED_SHUTDOWN_OUTCOME,
                "status": 503,
            },
            "codexhub_error": codexhub_error,
        }
    return {
        "type": USER_REQUESTED_SHUTDOWN_OUTCOME,
        "error": USER_REQUESTED_SHUTDOWN_OUTCOME,
        "detail": message,
        "codexhub_error": codexhub_error,
    }


def _record_user_requested_shutdown() -> None:
    write_proxy_event(
        "request_cancelled",
        shutdown_outcome=USER_REQUESTED_SHUTDOWN_OUTCOME,
        status=503,
        error=USER_REQUESTED_SHUTDOWN_OUTCOME,
        detail="Gateway shutdown requested by user",
    )


def _gateway_event_writer_recovery_record(
    summary: bounded_event_writer.RecoverySummary,
) -> Mapping[str, Any]:
    return proxy_telemetry.prepare_event_payload(
        "telemetry_writer_recovered",
        {
            "overflow_records": summary.overflow_records,
            "overflow_bytes": summary.overflow_bytes,
            "failed_records": summary.failed_records,
            "failure_count": summary.failure_count,
            "failure_categories": list(summary.failure_categories),
        },
        RUNTIME_CODEX_DIR,
    )


_previous_gateway_event_writer = globals().get("GATEWAY_EVENT_WRITER")
if isinstance(_previous_gateway_event_writer, bounded_event_writer.BoundedEventWriter):
    _previous_gateway_event_writer.shutdown(timeout=1.0)


GATEWAY_EVENT_WRITER = bounded_event_writer.BoundedEventWriter(
    bounded_event_writer.JsonlFileSink(PROXY_EVENT_LOG_PATH),
    max_records=GATEWAY_EVENT_QUEUE_MAX_RECORDS,
    max_bytes=GATEWAY_EVENT_QUEUE_MAX_BYTES,
    recovery_record_factory=_gateway_event_writer_recovery_record,
    thread_name="codex-gateway-event-writer",
)
# The compile-selected debug flavor will install a recorder in the later
# Tauri/runtime slice. Normal builds intentionally have no recorder object,
# settings toggle, or environment switch that could activate persistence.
GATEWAY_DIAGNOSTIC_RECORDER: diagnostic_recorder.DiagnosticRecorderProtocol | None = None
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300
DEFAULT_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS = 600.0
DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS = DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS
DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS = DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS
DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS = 180.0
DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS = 2
DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS = 30
DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS = 300.0
DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS = 600.0
DEFAULT_MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
RETRY_REQUEST_MAIN_GENERATION = "main_generation"
RETRY_REQUEST_COMPACT = "compact"
RETRY_REQUEST_IMAGE_PROXY_VISION = "image_proxy_vision"
RETRY_REQUEST_OFFICIAL_CONTROL = "official_control"
BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH = "official_codex_app_http_passthrough"
BEHAVIOR_OFFICIAL_GATEWAY_COMPAT = "official_gateway_compat"
BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY = "external_provider_gateway"
BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER = "codex_app_external_adapter"
BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED = "third_party_app_transparent_metered"

WIRE_TRANSPARENT = "transparent"
WIRE_RESPONSES_TO_CHAT = "responses_to_chat"
WIRE_CHAT_TO_RESPONSES = "chat_to_responses"

CODEX_SEMANTIC_EXTERNAL_ADAPTER = "codex_app_external_adapter"
CODEX_SEMANTIC_NONE = "none"

REQUEST_KIND_GATEWAY = "gateway"
REQUEST_KIND_TRANSPARENT = "transparent"

RETRY_GATEWAY_FULL = "gateway_full"
RETRY_CONSERVATIVE_PRE_OUTPUT = "conservative_pre_output"

USAGE_SYNC_CAPTURE = "sync_capture"
USAGE_ASYNC_TAP = "async_tap"

REPAIR_CODEX_SUBAGENT = "codex_subagent_repair"
REPAIR_NONE = "none"

VISION_PROXY_DISABLED = "disabled"
VISION_PROXY_CODEX_APP_ADAPTER = "codex_app_adapter"
VISION_PROXY_TRANSPARENT_OVERLAY = "transparent_overlay"

ROUTE_PLAN_SCHEMA_VERSION = "codexhub.route-plan.v1"


class CapabilityState(str, Enum):
    SUPPORTED = "Supported"
    UNSUPPORTED = "Unsupported"
    UNQUALIFIED = "Unqualified"


class RouteProtocol(str, Enum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    AUTO = "auto"
    UNKNOWN = "unknown"


class AttemptRequestBodyMode(str, Enum):
    PREPARED_DIRECT = "prepared_direct"
    CONVERT_RESPONSES_TO_CHAT = "convert_responses_to_chat"


class CallerRequestBodyMode(str, Enum):
    PRESERVE_CALLER = "preserve_caller"
    CONVERT_CHAT_TO_RESPONSES = "convert_chat_to_responses"


class AuthenticationStrategy(str, Enum):
    CODEX_AUTH = "codex_auth"
    API_KEY = "api_key"
    OLLAMA_API_KEY = "ollama_api_key"
    INCOMING = "incoming"
    UNKNOWN = "unknown"


class SensitiveValue:
    """An immutable secret whose representation and equality never expose value."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SensitiveValue(<redacted>)"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SensitiveValue)

    def __hash__(self) -> int:
        return hash(SensitiveValue)

    def __deepcopy__(self, memo: dict[int, Any]) -> SensitiveValue:
        return self


class OperationalAuthentication:
    """Request-scoped auth material captured after route viability is proved."""

    __slots__ = (
        "strategy",
        "_authorization",
        "_account_id",
        "_generated_session_id",
        "_generated_client_request_id",
    )

    def __init__(
        self,
        strategy: AuthenticationStrategy,
        *,
        authorization: str | None,
        account_id: str | None = None,
        generated_session_id: str | None = None,
        generated_client_request_id: str | None = None,
    ):
        object.__setattr__(self, "strategy", strategy)
        object.__setattr__(
            self,
            "_authorization",
            SensitiveValue(authorization) if authorization else None,
        )
        object.__setattr__(
            self,
            "_account_id",
            SensitiveValue(account_id) if account_id else None,
        )
        object.__setattr__(
            self,
            "_generated_session_id",
            (
                SensitiveValue(generated_session_id)
                if generated_session_id
                else None
            ),
        )
        object.__setattr__(
            self,
            "_generated_client_request_id",
            (
                SensitiveValue(generated_client_request_id)
                if generated_client_request_id
                else None
            ),
        )

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    @property
    def authorization(self) -> str | None:
        return (
            self._authorization.reveal()
            if self._authorization is not None
            else None
        )

    @property
    def account_id(self) -> str | None:
        return (
            self._account_id.reveal()
            if self._account_id is not None
            else None
        )

    @property
    def generated_session_id(self) -> str | None:
        return (
            self._generated_session_id.reveal()
            if self._generated_session_id is not None
            else None
        )

    @property
    def generated_client_request_id(self) -> str | None:
        return (
            self._generated_client_request_id.reveal()
            if self._generated_client_request_id is not None
            else None
        )

    def __repr__(self) -> str:
        return (
            "OperationalAuthentication("
            f"strategy={self.strategy!r}, materialized=True)"
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, OperationalAuthentication)
            and self.strategy == other.strategy
        )

    def __hash__(self) -> int:
        return hash((OperationalAuthentication, self.strategy))

    def __deepcopy__(
        self,
        memo: dict[int, Any],
    ) -> OperationalAuthentication:
        return self


class FrozenRequestHeaders:
    """Deeply immutable outbound headers with redacted values."""

    __slots__ = ("_items", "_materialized")

    def __init__(
        self,
        headers: Mapping[str, str] | None = None,
        *,
        materialized: bool,
    ):
        object.__setattr__(
            self,
            "_items",
            tuple(
                (str(name), SensitiveValue(str(value)))
                for name, value in (headers or {}).items()
            ),
        )
        object.__setattr__(self, "_materialized", materialized)

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    @classmethod
    def unmaterialized(cls) -> FrozenRequestHeaders:
        return cls(materialized=False)

    @property
    def materialized(self) -> bool:
        return self._materialized

    def to_dict(self) -> dict[str, str]:
        if not self._materialized:
            raise RuntimeError(
                "route attempt headers were not materialized before execution"
            )
        return {
            name: sensitive_value.reveal()
            for name, sensitive_value in self._items
        }

    def __repr__(self) -> str:
        return (
            "FrozenRequestHeaders("
            f"materialized={self._materialized}, count={len(self._items)})"
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, FrozenRequestHeaders)
            and self._materialized == other._materialized
            and tuple(name.lower() for name, _value in self._items)
            == tuple(name.lower() for name, _value in other._items)
        )

    def __hash__(self) -> int:
        return hash(
            (
                FrozenRequestHeaders,
                self._materialized,
                tuple(name.lower() for name, _value in self._items),
            )
        )

    def __deepcopy__(
        self,
        memo: dict[int, Any],
    ) -> FrozenRequestHeaders:
        return self


class ToolExposureMode(str, Enum):
    CURRENT_COMPATIBILITY = "current_compatibility"
    OFFICIAL_NATIVE = "official_native"
    NATIVE_DEFERRED_SEARCH_CANDIDATE = "native_deferred_search_candidate"
    NATIVE_NO_SEARCH_CANDIDATE = "native_no_search_candidate"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class VisionAction(str, Enum):
    PASS_THROUGH = "pass_through"
    PROXY = "proxy"
    REJECT = "reject"


class VisionNetworkAction(str, Enum):
    NONE = "none"
    IMAGE_PROXY = "image_proxy"


class CollaborationBackend(str, Enum):
    CODEX_RUNTIME = "codex_runtime"
    CLIENT_RUNTIME = "client_runtime"
    GATEWAY_COMPATIBILITY = "gateway_compatibility"


class CodexCompatibilityPolicy(str, Enum):
    OFFICIAL_NATIVE = "official_native"
    CURRENT_COMPATIBILITY = "current_compatibility"
    NONE = "none"


class ExecutionOwner(str, Enum):
    CODEX_CLIENT = "codex_client"


class StreamingPolicy(str, Enum):
    OFFICIAL_PASSTHROUGH = "official_passthrough"
    TRANSPARENT = "transparent"
    TRANSPARENT_CONVERTED = "transparent_converted"
    GATEWAY_ADAPTED = "gateway_adapted"


class RetryPolicy(str, Enum):
    GATEWAY_FULL = RETRY_GATEWAY_FULL
    CONSERVATIVE_PRE_OUTPUT = RETRY_CONSERVATIVE_PRE_OUTPUT


class UsagePolicy(str, Enum):
    SYNC_CAPTURE = USAGE_SYNC_CAPTURE
    ASYNC_TAP = USAGE_ASYNC_TAP


class TransportPolicy(str, Enum):
    OFFICIAL_KEEPALIVE = "official_keepalive"
    STANDARD = "standard"


class MutationPolicy(str, Enum):
    OFFICIAL_PASSTHROUGH = "official_passthrough"
    TRANSPARENT = "transparent"
    GATEWAY_COMPATIBILITY = "gateway_compatibility"


class RouteMutation(str, Enum):
    MODEL_ALIAS = "model_alias"
    NAMESPACE_FLATTENING = "namespace_flattening"
    WIRE_CONVERSION = "wire_conversion"
    SEMANTIC_REPAIR = "semantic_repair"
    HARD_CODED_SCHEMA_INJECTION = "hard_coded_schema_injection"
    OFFICIAL_TOOL_SEARCH_PRESERVATION = "official_tool_search_preservation"
    SYNTHETIC_TERMINAL_FAILURE = "synthetic_terminal_failure"
    IMAGE_CONTENT_REPLACEMENT = "image_content_replacement"
    IMAGE_UNSUPPORTED_REJECTION = "image_unsupported_rejection"
    CALLER_TOOL_STRIPPING = "caller_tool_stripping"
    UNSUPPORTED_PROTOCOL_REJECTION = "unsupported_protocol_rejection"


RETRY_FAILURE_QUICK_TRANSIENT = "quick_transient"
RETRY_FAILURE_PROVIDER_THROTTLE = "provider_throttle"
RETRY_FAILURE_PROVIDER_OVERLOADED = "provider_overloaded"
RETRY_FAILURE_PERMANENT = "permanent"
CAPACITY_RETRY_FAILURE_CLASSES = {
    RETRY_FAILURE_PROVIDER_THROTTLE,
    RETRY_FAILURE_PROVIDER_OVERLOADED,
}
CAPACITY_RETRY_CADENCE_SECONDS = (10, 20, 30, 60)
TRANSIENT_HTTP_RETRY_STATUSES = {408, 409, 421, 425, 429, 500, 502, 503, 504}
AUTO_UPSTREAM_PROTOCOL_FALLBACK_STATUSES = {404, 405, 415, 422}

RETRY_SAFETY_SAFE_PREWRITE = "safe_prewrite"
RETRY_SAFETY_GUARANTEED_IDEMPOTENT = "guaranteed_idempotent"
RETRY_SAFETY_SUPPRESSED_POST_WRITE = "suppressed_post_write"
RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE = "suppressed_post_exposure"
RETRY_SAFETY_UNKNOWN = "unknown"
PERMANENT_HTTP_ERROR_STATUSES = {
    400,
    401,
    403,
    404,
    405,
    406,
    407,
    410,
    411,
    412,
    413,
    414,
    415,
    416,
    417,
    418,
    422,
    426,
    428,
    431,
    451,
    501,
    505,
}
PERMANENT_UPSTREAM_ERROR_VALUES = {
    "400",
    "401",
    "402",
    "403",
    "404",
    "405",
    "406",
    "410",
    "413",
    "414",
    "415",
    "422",
    "451",
    "10003",
    "10004",
    "10005",
    "10013",
    "10014",
    "10015",
    "10016",
    "10019",
    "10163",
    "10404",
    "10907",
    "10910",
    "11200",
    "11201",
    "11221",
    "access_denied",
    "accessdeniedexception",
    "authentication_error",
    "bad_request",
    "badrequest",
    "billing_hard_limit_reached",
    "billing_not_active",
    "blocked_by_guardrail",
    "content_filter",
    "content_policy_violation",
    "context_length_exceeded",
    "forbidden",
    "guardrail_block",
    "incorrect_api_key",
    "insufficient_quota",
    "insufficient_balance",
    "insufficient_credits",
    "invalid_argument",
    "invalidargument",
    "invalid_api_key",
    "invalid_image",
    "invalid_key",
    "invalid_parameter",
    "invalid_parameters",
    "invalid_request",
    "invalid_request_error",
    "moderation",
    "model_not_found",
    "not_found_error",
    "payment_required",
    "permission_denied",
    "permission_error",
    "safety_violation",
    "unauthorized",
    "unsupported_image",
    "unsupported_parameter",
    "unsupported_country",
    "unsupported_value",
    "validation_error",
    "validationexception",
}
PERMANENT_UPSTREAM_ERROR_NEEDLES = (
    "billing",
    "content policy",
    "context length",
    "context_length",
    "country not supported",
    "incorrect api key",
    "insufficient balance",
    "insufficient credits",
    "insufficient quota",
    "invalid api key",
    "invalid argument",
    "invalid parameter",
    "maximum context",
    "moderation",
    "payment required",
    "permission denied",
    "safety",
    "schema",
    "sensitive",
    "token limit",
    "tokens exceed",
    "too many tokens",
    "unsupported country",
    "validation error",
    "token数量超过上限",
)
PERMANENT_UPSTREAM_AUTH_NEEDLES = (
    "access denied",
    "forbidden",
    "not authorized",
    "unauthorized",
)
PROVIDER_THROTTLE_ERROR_VALUES = {
    "10007",
    "11202",
    "11203",
    "11210",
    "429",
    "rate_limit",
    "rate_limit_error",
    "rate_limit_exceeded",
    "rate_limit_reached",
    "resource_exhausted",
    "request_throttled",
    "throttled",
    "throttling",
    "throttlingexception",
    "too_many_requests",
}
PROVIDER_THROTTLE_ERROR_NEEDLES = (
    "limit_requests",
    "qps",
    "rate limit",
    "rate_limit",
    "request limit",
    "requests per minute",
    "requests rate",
    "resource exhausted",
    "rpm",
    "rps",
    "throttl",
    "tokens per minute",
    "too many requests",
    "tpm",
    "流控",
    "限流",
)
PROVIDER_OVERLOADED_ERROR_VALUES = {
    "10008",
    "10009",
    "10010",
    "10011",
    "10012",
    "10110",
    "10222",
    "10223",
    "503",
    "529",
    "model_overloaded",
    "overloaded_error",
    "provider_unavailable",
    "server_overloaded",
    "service_unavailable",
    "serviceunavailable",
    "serviceunavailableexception",
    "unavailable",
}
PROVIDER_OVERLOADED_ERROR_NEEDLES = (
    "capacity",
    "engine node",
    "engineinternalerror",
    "invalid response",
    "lb",
    "model is down",
    "no available model provider",
    "overload",
    "overloaded",
    "queue",
    "queued",
    "server overloaded",
    "service unavailable",
    "system is busy",
    "temporarily unavailable",
    "引擎节点",
    "排队",
    "服务忙",
)
IMAGE_PROXY_PROMPT_VERSION = "v3"
IMAGE_PROXY_PROMPT = (
    "Describe the image for a downstream text-only coding agent that cannot see it. "
    "Be faithful and evidence-first. Include the scene, important objects, layout, "
    "colors, and spatial relationships. Transcribe all visible text with OCR, including "
    "UI labels, buttons, menus, dialogs, errors, warnings, code, URLs, numbers, and "
    "timestamps. For screenshots, describe UI state, selected items, disabled controls, "
    "notifications, and error messages. For charts or tables, summarize axes, legends, "
    "series, rows, columns, units, and visible trends or outliers. Mark ambiguous or "
    "unreadable details explicitly instead of guessing. Return only compact plain prose; "
    "do not include reasoning, caveats about being a proxy, or meta commentary."
)
IMAGE_PROXY_PROGRESS_TEXT = "Analyzing image...\n\n"

logger = logging.getLogger("codex_proxy")
IMAGE_PROXY_CACHE_PATH = RUNTIME_PROXY_DIR / "image-proxy-cache.sqlite"
IMAGE_PROXY_CACHE_LOCK = threading.Lock()


class ImageProxyError(Exception):
    """Raised when a Vision Proxy request cannot be prepared safely."""


class ModelIdentityResolutionError(ValueError):
    """Raised when an exact provider/model pair cannot be proven safe.

    ``classification`` is deliberately a small, non-secret vocabulary used by
    diagnostics and callers.  ``catalog_inconsistency`` means the published
    snapshot itself is contradictory or ambiguous; ``local_resolution_failure``
    means the requested identity is absent, internal, stale, or unsupported.
    """

    CLASSIFICATIONS = frozenset({"catalog_inconsistency", "local_resolution_failure"})

    def __init__(
        self,
        message: str,
        *,
        classification: str,
        reason: str,
        provider_id: str | None = None,
        model_slug: str | None = None,
    ) -> None:
        if classification not in self.CLASSIFICATIONS:
            raise ValueError(f"unsupported model identity classification: {classification}")
        self.classification = classification
        self.reason = reason
        self.provider_id = provider_id
        self.model_slug = model_slug
        super().__init__(message)


class UnsupportedRouteProtocolError(ValueError):
    """Raised when a configured route has no executable protocol attempt."""


class UnqualifiedRouteProtocolError(ValueError):
    """Raised when a configured route protocol has no qualified identity."""


class CompactEmptyResponseError(RuntimeError):
    """Raised when a compact request succeeds with no summary text."""

    def __init__(self, upstream_name: str):
        self.upstream_name = upstream_name
        super().__init__("Upstream returned an empty compact summary.")


class LifecycleEmptyFinalResponseError(RuntimeError):
    """Raised when a completed subagent lifecycle ends with no visible final text."""

    def __init__(self, upstream_name: str):
        self.upstream_name = upstream_name
        super().__init__("Upstream returned an empty final response after completed subagent lifecycle.")


class LifecycleFinalFormatResponseError(RuntimeError):
    """Raised when a completed subagent lifecycle emits a final report with extra prose."""

    def __init__(self, upstream_name: str):
        self.upstream_name = upstream_name
        super().__init__("Upstream returned a final response that did not start with the requested report format.")




class UpstreamStreamIdleTimeoutError(TimeoutError):
    """Raised when an upstream SSE stream stalls before completion."""

    def __init__(self, timeout_seconds: float, phase: str = "model_event"):
        self.timeout_seconds = timeout_seconds
        self.phase = phase
        if phase == "transport":
            detail = "without upstream bytes"
        elif phase == "model_event":
            detail = "without a valid model event"
        else:
            detail = "before output started" if phase == "pre_output" else "after output started"
        super().__init__(f"Upstream stream stalled for {timeout_seconds:g} seconds {detail}.")


class GatewayPreResponseBudgetExhausted(TimeoutError):
    """Raised when main generation cannot reach a usable response within its shared budget."""

    def __init__(
        self,
        *,
        phase: str = "pre_response",
        attempt: int | None = None,
        budget_seconds: float = DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS,
    ):
        self.phase = phase
        self.attempt = attempt
        self.budget_seconds = budget_seconds
        super().__init__("Gateway pre-response budget exhausted before a usable upstream response.")


def upstream_timeout_seconds() -> int:
    settings_value = _runtime_settings_value("gateway_request_timeout_seconds")
    if isinstance(settings_value, int):
        return settings_value if settings_value > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    if isinstance(settings_value, str):
        try:
            value = int(settings_value)
        except ValueError:
            value = DEFAULT_UPSTREAM_TIMEOUT_SECONDS
        return value if value > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    raw_value = os.environ.get("CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS


def sse_keepalive_seconds() -> float:
    raw_value = os.environ.get("CODEX_PROXY_SSE_KEEPALIVE_SECONDS")
    if not raw_value:
        return 15.0
    try:
        value = float(raw_value)
    except ValueError:
        return 15.0
    if value <= 0:
        return 0.0
    return max(0.001, min(value, 60.0))


def _number_setting_or_env(
    *,
    settings_name: str,
    env_name: str,
    default: float,
    fallback_settings_names: tuple[str, ...] = (),
    fallback_env_names: tuple[str, ...] = (),
) -> float:
    def parse_setting(name: str) -> float | None:
        settings_value = _runtime_settings_value(name)
        if isinstance(settings_value, (int, float)) and not isinstance(settings_value, bool):
            return float(settings_value) if settings_value > 0 else 0.0
        if isinstance(settings_value, str):
            try:
                value = float(settings_value)
            except ValueError:
                return None
            return value if value > 0 else 0.0
        return None

    def parse_env(name: str) -> float | None:
        raw_value = os.environ.get(name)
        if raw_value is None:
            return None
        try:
            value = float(raw_value)
        except ValueError:
            return None
        return value if value > 0 else 0.0

    primary_setting = parse_setting(settings_name)
    if primary_setting is not None:
        return primary_setting
    primary_env = parse_env(env_name)
    if primary_env is not None:
        return primary_env
    for name in fallback_settings_names:
        fallback_setting = parse_setting(name)
        if fallback_setting is not None:
            return fallback_setting
    for name in fallback_env_names:
        fallback_env = parse_env(name)
        if fallback_env is not None:
            return fallback_env
    return default


def transport_sse_idle_timeout_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_transport_sse_idle_timeout_seconds",
        env_name="CODEX_PROXY_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS",
        default=DEFAULT_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS,
    )


def model_event_sse_idle_timeout_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_model_event_sse_idle_timeout_seconds",
        env_name="CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS",
        default=DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS,
        fallback_settings_names=(
            "gateway_post_content_sse_idle_timeout_seconds",
            "gateway_pre_output_sse_idle_timeout_seconds",
        ),
        fallback_env_names=(
            "CODEX_PROXY_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS",
            "CODEX_PROXY_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS",
        ),
    )


def pre_output_sse_idle_timeout_seconds() -> float:
    settings_value = _runtime_settings_value("gateway_pre_output_sse_idle_timeout_seconds")
    if isinstance(settings_value, (int, float)) and not isinstance(settings_value, bool):
        return float(settings_value) if settings_value > 0 else 0.0
    if isinstance(settings_value, str):
        try:
            value = float(settings_value)
        except ValueError:
            value = DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS
        return value if value > 0 else 0.0
    raw_value = os.environ.get("CODEX_PROXY_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS
    return value if value > 0 else 0.0


def post_content_sse_idle_timeout_seconds() -> float:
    settings_value = _runtime_settings_value("gateway_post_content_sse_idle_timeout_seconds")
    if isinstance(settings_value, (int, float)) and not isinstance(settings_value, bool):
        return float(settings_value) if settings_value > 0 else 0.0
    if isinstance(settings_value, str):
        try:
            value = float(settings_value)
        except ValueError:
            value = DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS
        return value if value > 0 else 0.0
    raw_value = os.environ.get("CODEX_PROXY_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS
    try:
        value = float(raw_value)
    except ValueError:
        return DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS
    return value if value > 0 else 0.0


def official_upstream_open_attempts() -> int:
    raw_value = os.environ.get("CODEX_PROXY_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS")
    if not raw_value:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    if value <= 0:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    return min(value, DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS)


def _env_flag(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}


def _runtime_settings_value(name: str) -> Any:
    try:
        with (RUNTIME_PROXY_DIR / "settings.json").open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return payload.get(name)


def gateway_client_key() -> str | None:
    raw_value = os.environ.get("CODEX_PROXY_GATEWAY_CLIENT_KEY")
    if raw_value is None:
        return None
    value = raw_value.strip()
    return value or None


def max_request_body_bytes() -> int:
    raw_value = os.environ.get("CODEX_PROXY_MAX_REQUEST_BODY_BYTES")
    if raw_value is None:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    if value <= 0:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    return min(value, 256 * 1024 * 1024)


def _env_or_settings_flag(env_name: str, settings_name: str, default: bool) -> bool:
    settings_value = _runtime_settings_value(settings_name)
    if isinstance(settings_value, bool):
        return settings_value
    if isinstance(settings_value, str):
        return settings_value.strip().lower() not in {"0", "false", "no", "off", ""}
    raw_value = os.environ.get(env_name)
    if raw_value is not None:
        return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}
    return default


def gateway_auto_retry_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_AUTO_RETRY_ENABLED",
        "gateway_auto_retry_enabled",
        True,
    )


def gateway_official_http_passthrough_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED",
        "gateway_official_http_passthrough_enabled",
        True,
    )


def gateway_websocket_recorder_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_WEBSOCKET_RECORDER_ENABLED",
        "gateway_websocket_recorder_enabled",
        False,
    )


def gateway_websocket_recorder_max_frames() -> int:
    value = _number_setting_or_env(
        settings_name="gateway_websocket_recorder_max_frames",
        env_name="CODEX_PROXY_WEBSOCKET_RECORDER_MAX_FRAMES",
        default=8,
    )
    return max(1, min(int(value), 32))


def gateway_websocket_recorder_idle_timeout_seconds() -> float:
    value = _number_setting_or_env(
        settings_name="gateway_websocket_recorder_idle_timeout_seconds",
        env_name="CODEX_PROXY_WEBSOCKET_RECORDER_IDLE_TIMEOUT_SECONDS",
        default=2.0,
    )
    return max(0.1, min(float(value), 30.0))


def gateway_auto_retry_max_attempts() -> int:
    settings_value = _runtime_settings_value("gateway_auto_retry_max_attempts")
    if isinstance(settings_value, int):
        return max(1, min(settings_value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    if isinstance(settings_value, str):
        try:
            value = int(settings_value)
        except ValueError:
            value = DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS
        return max(1, min(value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    raw_value = os.environ.get("CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS")
    if not raw_value:
        return DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS
    return max(1, min(value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))


def gateway_capacity_retry_elapsed_limit_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_capacity_retry_elapsed_limit_seconds",
        env_name="CODEX_PROXY_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS",
        default=DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS,
    )


def gateway_stream_retry_elapsed_limit_seconds() -> float:
    return _number_setting_or_env(
        settings_name="gateway_stream_retry_elapsed_limit_seconds",
        env_name="CODEX_PROXY_STREAM_RETRY_ELAPSED_LIMIT_SECONDS",
        default=DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS,
    )


def gateway_downstream_retry_notice_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_DOWNSTREAM_RETRY_NOTICE_ENABLED",
        "gateway_downstream_retry_notice_enabled",
        False,
    )


def gateway_capacity_retry_delay_seconds(attempt: int) -> int:
    index = max(1, attempt) - 1
    if index < len(CAPACITY_RETRY_CADENCE_SECONDS):
        return CAPACITY_RETRY_CADENCE_SECONDS[index]
    return CAPACITY_RETRY_CADENCE_SECONDS[-1]


def subagent_assist_mode() -> str:
    return _subagent_policy_assist_mode()


def subagent_guidance_enabled(event_context: Mapping[str, Any] | None) -> bool:
    return _subagent_policy_guidance_enabled(event_context)


def subagent_semantic_repair_enabled(event_context: Mapping[str, Any] | None) -> bool:
    return _subagent_policy_semantic_repair_enabled(event_context)


def lifecycle_empty_final_resample_enabled(
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> bool:
    if request_kind != RETRY_REQUEST_MAIN_GENERATION:
        return False
    if not subagent_semantic_repair_enabled(event_context):
        return False
    return bool((event_context or {}).get("subagent_lifecycle_complete"))


def gateway_retry_delay_seconds(
    attempt: int,
    *,
    failure_class: str = RETRY_FAILURE_QUICK_TRANSIENT,
    exc: BaseException | None = None,
) -> int:
    retry_after_seconds = _retry_after_delay_seconds(exc)
    if retry_after_seconds is not None:
        return retry_after_seconds
    if failure_class == RETRY_FAILURE_PROVIDER_THROTTLE:
        return gateway_capacity_retry_delay_seconds(attempt)
    return min(max(1, attempt - 1) * 2, 8)


def gateway_image_proxy_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_IMAGE_PROXY_ENABLED",
        "gateway_image_proxy_enabled",
        False,
    )


def gateway_image_proxy_model() -> str:
    settings_value = _runtime_settings_value("gateway_image_proxy_model")
    if isinstance(settings_value, str) and settings_value.strip():
        return settings_value.strip()
    return os.environ.get("CODEX_PROXY_IMAGE_PROXY_MODEL", "").strip()


def _observe_gateway_diagnostic(method: str, *args: Any, **kwargs: Any) -> None:
    """Keep optional recorder failures out of Gateway request behavior."""

    recorder = GATEWAY_DIAGNOSTIC_RECORDER
    if recorder is None:
        return
    try:
        observation = getattr(recorder, method, None)
        if callable(observation):
            observation(*args, **kwargs)
    except Exception:
        return


def _diagnostic_context_value(event_context: Mapping[str, Any] | None, key: str) -> Any:
    """Read optional diagnostic context without changing a request on failure."""

    if event_context is None:
        return None
    try:
        return event_context.get(key)
    except Exception:
        return None


def _diagnostic_response_metadata(response: Any) -> tuple[Any, Any]:
    """Snapshot only optional header summaries for the failure-contained hook."""

    try:
        status = getattr(response, "status", None)
        if not status:
            status = getattr(response, "code", None)
    except Exception:
        status = None
    try:
        headers = getattr(response, "headers", None)
    except Exception:
        headers = None
    return status, headers


def _diagnostic_connection_disposition(response: Any) -> str:
    try:
        disposition = getattr(response, "connection_disposition", "unobserved")
    except Exception:
        return "unobserved"
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _diagnostic_error_connection_disposition(exc: BaseException) -> str:
    """Read only the safe lease label attached to an Official transport error."""

    try:
        disposition = getattr(exc, "_codexhub_diagnostic_connection_disposition", "unobserved")
    except Exception:
        return "unobserved"
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _diagnostic_transport_phase(failure_phase: str | None) -> str | None:
    return {
        "dns": "upstream_dns",
        "tcp_connect": "upstream_tcp",
        "tls": "upstream_tls",
        "request_write": "upstream_request_write",
    }.get(failure_phase)


def write_proxy_event(event: str, **fields: Any) -> None:
    public_fields = _public_event_context(fields)
    _observe_gateway_diagnostic("observe_proxy_event", event, public_fields)
    payload = proxy_telemetry.prepare_event_payload(event, public_fields, RUNTIME_CODEX_DIR)
    _enqueue_gateway_event_payload(payload)


def _enqueue_gateway_event_payload(payload: Mapping[str, Any]) -> bool:
    return GATEWAY_EVENT_WRITER.enqueue(payload)


def flush_proxy_event_writer(timeout: float = 5.0) -> bool:
    return GATEWAY_EVENT_WRITER.flush(timeout).completed


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _usage_nested_int(usage: Mapping[str, Any], object_key: str, value_key: str) -> int | None:
    value = usage.get(object_key)
    if not isinstance(value, Mapping):
        return None
    return _usage_int(value.get(value_key))


def _normalize_usage_for_event(
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str = "upstream_missing_usage",
) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        return {
            "usage_source": "missing",
            "usage_missing_reason": missing_reason,
        }

    input_tokens = _usage_int(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _usage_int(usage.get("prompt_tokens"))
    output_tokens = _usage_int(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _usage_int(usage.get("completion_tokens"))
    total_tokens = _usage_int(usage.get("total_tokens"))
    cached_input_tokens = _usage_nested_int(usage, "input_tokens_details", "cached_tokens")
    if cached_input_tokens is None:
        cached_input_tokens = _usage_nested_int(usage, "prompt_tokens_details", "cached_tokens")
    reasoning_tokens = _usage_nested_int(usage, "output_tokens_details", "reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = _usage_nested_int(usage, "completion_tokens_details", "reasoning_tokens")

    fields: dict[str, Any] = {"usage_source": "upstream"}
    if input_tokens is not None:
        fields["usage_input_tokens"] = input_tokens
    if output_tokens is not None:
        fields["usage_output_tokens"] = output_tokens
    if total_tokens is not None:
        fields["usage_total_tokens"] = total_tokens
    elif input_tokens is not None and output_tokens is not None:
        fields["usage_total_tokens"] = input_tokens + output_tokens
    if cached_input_tokens is not None:
        fields["usage_cached_input_tokens"] = cached_input_tokens
    if reasoning_tokens is not None:
        fields["usage_reasoning_tokens"] = reasoning_tokens
    if len(fields) == 1:
        return {
            "usage_source": "missing",
            "usage_missing_reason": "upstream_usage_unrecognized",
        }
    return fields


def _usage_from_payload(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    usage = payload.get("usage")
    return usage if isinstance(usage, Mapping) else None


def _usage_from_json_body(body: bytes) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _usage_from_payload(payload)


def _usage_from_response_event(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if event.get("type") != "response.completed":
        return None
    response = event.get("response")
    return _usage_from_payload(response)


def _capture_usage(
    usage_capture: dict[str, Any] | None,
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str = "upstream_missing_usage",
) -> None:
    if usage_capture is None:
        return
    if usage_capture.get("usage_source") == "upstream":
        return
    usage_capture.clear()
    usage_capture.update(_normalize_usage_for_event(usage, missing_reason=missing_reason))


def _public_event_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (context or {}).items()
        if not str(key).startswith("_")
    }


_FAILURE_EVENT_CONTEXT_FIELD_ALLOWLIST = frozenset(
    {
        "behavior_profile",
        "client_id",
        "client_inference_source",
        "inbound_format",
        "model",
        "model_canonical",
        "model_requested",
        "provider_hint",
        "request_id",
        "request_kind",
        "route_attempt_fallback_http_statuses",
        "route_attempt_index",
        "route_attempt_endpoint_url",
        "route_attempt_model_canonical",
        "route_attempt_model_requested",
        "route_attempt_mutation_summary",
        "route_attempt_protocol",
        "route_attempt_provider_id",
        "route_attempt_upstream_model",
        "route_endpoint_url",
        "route_model_canonical",
        "route_model_requested",
        "route_provider_id",
        "route_mode",
        "route_reason",
        "route_upstream_model",
        "upstream_format",
    }
)

_FAILURE_EVENT_CONTEXT_BOUNDED_LIST_FIELDS = frozenset(
    {
        "route_attempt_fallback_http_statuses",
        "route_attempt_mutation_summary",
    }
)


def _bounded_failure_event_context(
    context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Keep only bounded routing metadata on failure-classification events."""

    bounded: dict[str, Any] = {}
    for key, value in (context or {}).items():
        if key not in _FAILURE_EVENT_CONTEXT_FIELD_ALLOWLIST:
            continue
        if isinstance(value, str):
            bounded[key] = value[:200]
        elif isinstance(value, (bool, int, float)) or value is None:
            bounded[key] = value
        elif (
            key in _FAILURE_EVENT_CONTEXT_BOUNDED_LIST_FIELDS
            and isinstance(value, (list, tuple))
        ):
            bounded[key] = [
                item[:80] if isinstance(item, str) else item
                for item in value[:32]
                if isinstance(item, (str, bool, int, float)) or item is None
            ]
    return bounded


def _route_failure_event_fields(
    context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return only route identity fields for direct failure events.

    Several stream failure branches already pass request/model/status fields
    explicitly.  Keeping this helper route-only avoids duplicate keyword
    arguments while preserving the exact provider/model/endpoint identity.
    """

    return {
        key: value
        for key, value in _bounded_failure_event_context(context).items()
        if key.startswith("route_")
    }


def _usage_observed_context(
    event_context: Mapping[str, Any] | None,
    *,
    request_id: str | None,
    model: str | None,
    upstream: str,
    upstream_format: str,
    inbound_format: str,
) -> dict[str, Any]:
    context = _public_event_context(event_context)
    context.update(
        {
            "request_id": request_id,
            "model": model,
            "upstream": upstream,
            "upstream_format": upstream_format,
            "inbound_format": inbound_format,
        }
    )
    return context


def _write_usage_observed_event(
    context: Mapping[str, Any],
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str | None = None,
) -> None:
    if usage is None:
        if missing_reason is None:
            return
        usage_fields = _normalize_usage_for_event(None, missing_reason=missing_reason)
    else:
        usage_fields = _normalize_usage_for_event(usage)
    write_proxy_event(
        "usage_observed",
        request_id=context.get("request_id"),
        model=context.get("model"),
        model_requested=context.get("model_requested"),
        model_canonical=context.get("model_canonical"),
        upstream=context.get("upstream"),
        provider_id=context.get("provider_id") or context.get("upstream"),
        upstream_format=context.get("upstream_format"),
        inbound_format=context.get("inbound_format"),
        route_mode=context.get("route_mode"),
        client_id=context.get("client_id"),
        client_inference_source=context.get("client_inference_source"),
        **usage_fields,
    )


def _write_usage_observed_body_event(context: Mapping[str, Any], body: bytes) -> None:
    usage = _usage_from_json_body(body)
    _write_usage_observed_event(
        context,
        usage,
        missing_reason="upstream_missing_usage",
    )


OFFICIAL_PASSTHROUGH_USAGE_QUEUE: queue.Queue[tuple[dict[str, Any], bytes]] = queue.Queue(maxsize=2048)
_OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED = False
_OFFICIAL_PASSTHROUGH_USAGE_WORKER_LOCK = threading.Lock()
USAGE_OBSERVED_QUEUE: queue.Queue[tuple[str, dict[str, Any], bytes, str | None]] = queue.Queue(maxsize=2048)
_USAGE_OBSERVED_WORKER_STARTED = False
_USAGE_OBSERVED_WORKER_LOCK = threading.Lock()


def _start_official_passthrough_usage_worker() -> None:
    global _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED
    if _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED:
        return
    with _OFFICIAL_PASSTHROUGH_USAGE_WORKER_LOCK:
        if _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED:
            return
        threading.Thread(
            target=_official_passthrough_usage_worker,
            name="codex-proxy-official-usage",
            daemon=True,
        ).start()
        _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED = True


def _offer_official_passthrough_usage_line(context: Mapping[str, Any], line: bytes) -> None:
    if not line.startswith(b"data:"):
        return
    _start_official_passthrough_usage_worker()
    try:
        OFFICIAL_PASSTHROUGH_USAGE_QUEUE.put_nowait((_public_event_context(context), line))
    except queue.Full:
        return


def _official_passthrough_usage_worker() -> None:
    while True:
        context, line = OFFICIAL_PASSTHROUGH_USAGE_QUEUE.get()
        try:
            payload_bytes = _sse_payload_bytes(line)
            if payload_bytes is None:
                continue
            try:
                payload = json.loads(payload_bytes.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            usage = _usage_from_response_event(payload)
            if usage is None:
                continue
            _write_usage_observed_event(context, usage)
        finally:
            OFFICIAL_PASSTHROUGH_USAGE_QUEUE.task_done()


def _start_usage_observed_worker() -> None:
    global _USAGE_OBSERVED_WORKER_STARTED
    if _USAGE_OBSERVED_WORKER_STARTED:
        return
    with _USAGE_OBSERVED_WORKER_LOCK:
        if _USAGE_OBSERVED_WORKER_STARTED:
            return
        threading.Thread(
            target=_usage_observed_worker,
            name="codex-proxy-usage-observed",
            daemon=True,
        ).start()
        _USAGE_OBSERVED_WORKER_STARTED = True


def _offer_usage_observed_body(context: Mapping[str, Any], body: bytes) -> None:
    if not body:
        return
    _start_usage_observed_worker()
    try:
        USAGE_OBSERVED_QUEUE.put_nowait(("body", _public_event_context(context), body, None))
    except queue.Full:
        return


def _offer_usage_observed_sse_line(
    context: Mapping[str, Any],
    line: bytes,
    *,
    upstream_format: str,
) -> None:
    if not line.startswith(b"data:"):
        return
    _start_usage_observed_worker()
    try:
        USAGE_OBSERVED_QUEUE.put_nowait(("sse", _public_event_context(context), line, upstream_format))
    except queue.Full:
        return


def _usage_observed_worker() -> None:
    while True:
        item_type, context, payload_bytes, upstream_format = USAGE_OBSERVED_QUEUE.get()
        try:
            usage: Mapping[str, Any] | None = None
            if item_type == "body":
                _write_usage_observed_body_event(context, payload_bytes)
                continue
            elif item_type == "sse":
                payload = None
                sse_payload_bytes = _sse_payload_bytes(payload_bytes)
                if sse_payload_bytes is not None and sse_payload_bytes != b"[DONE]":
                    try:
                        payload = json.loads(sse_payload_bytes.decode("utf-8-sig"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = None
                if isinstance(payload, Mapping):
                    usage = (
                        _usage_from_payload(payload)
                        if upstream_format == "chat_completions"
                        else _usage_from_response_event(payload)
                    )
            _write_usage_observed_event(context, usage)
        finally:
            USAGE_OBSERVED_QUEUE.task_done()


def _write_adapter_event(event_context: Mapping[str, Any] | None, event: str, **fields: Any) -> None:
    if event_context is None:
        return
    payload = _public_event_context(event_context)
    payload.update(fields)
    write_proxy_event(event, **payload)


def _write_failure_event(
    event_context: Mapping[str, Any] | None,
    event: str,
    **fields: Any,
) -> None:
    _write_adapter_event(
        _bounded_failure_event_context(event_context),
        event,
        **fields,
    )


def _raise_collaboration_boundary_error(
    event_context: Mapping[str, Any] | None,
    *,
    classification: str,
    message: str,
    surface: str = "request",
    cause: BaseException | None = None,
) -> NoReturn:
    write_proxy_event(
        "collaboration_boundary_rejected",
        surface=surface,
        outcome="rejected",
        count=1,
    )
    error = UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            COLLABORATION_BOUNDARY_ERROR_CODE,
            message,
        )
    )
    if cause is not None:
        raise error from cause
    raise error


def _resolve_collaboration_boundary(
    payload: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str = "request",
) -> str | None:
    if surface != "request":
        try:
            protocol = _classify_collaboration_payload(payload)
        except _CollaborationBoundaryError as exc:
            _raise_collaboration_boundary_error(
                event_context,
                classification=exc.classification,
                message="Collaboration protocol boundary is malformed or ambiguous.",
                surface=surface,
                cause=exc,
            )
        context_protocol = (
            event_context.get("collaboration_protocol")
            if isinstance(event_context, Mapping)
            else None
        )
        if context_protocol is not None and context_protocol not in {
            _COLLABORATION_V1,
            _COLLABORATION_V2,
        }:
            _raise_collaboration_boundary_error(
                event_context,
                classification="unknown_state",
                message="Collaboration protocol selection is unknown.",
                surface=surface,
            )
        if (
            context_protocol is not None
            and protocol is not None
            and protocol != context_protocol
        ):
            _raise_collaboration_boundary_error(
                event_context,
                classification="conflicting_selection",
                message="Collaboration protocol selection conflicts with the response.",
                surface=surface,
            )
    else:
        try:
            request_boundary = {
                "tools": payload.get("tools", []),
                "tool_choice": payload.get("tool_choice"),
            } if isinstance(payload, Mapping) else {"tools": [], "tool_choice": None}
            if isinstance(payload, Mapping):
                for key in ("multi_agent_version", "metadata", "features", "client_metadata"):
                    if key in payload:
                        request_boundary[key] = payload[key]
            current_protocol = _classify_collaboration_payload(request_boundary)

            raw_context_protocol = (
                event_context.get("collaboration_protocol")
                if isinstance(event_context, Mapping)
                else None
            )
            context_protocol = raw_context_protocol if raw_context_protocol in {
                _COLLABORATION_V1,
                _COLLABORATION_V2,
            } else None
            history_protocols = _collaboration_protocols(
                {"input": payload.get("input", [])}
                if isinstance(payload, Mapping)
                else {"input": []}
            )
            protocol = (
                current_protocol
                or context_protocol
            )
            if (
                current_protocol is not None
                and context_protocol is not None
                and current_protocol != context_protocol
            ):
                _raise_collaboration_boundary_error(
                    event_context,
                    classification="conflicting_selection",
                    message="Collaboration protocol selection conflicts with the request.",
                    surface=surface,
                )
            if len(history_protocols) > 1:
                _raise_collaboration_boundary_error(
                    event_context,
                    classification="mixed_v1_v2",
                    message="Collaboration history contains multiple protocol families.",
                    surface=surface,
                )
            history_protocol = next(iter(history_protocols), None)
            if (
                protocol is not None
                and history_protocol is not None
                and protocol != history_protocol
            ):
                _raise_collaboration_boundary_error(
                    event_context,
                    classification="conflicting_selection",
                    message="Collaboration protocol selection conflicts with history.",
                    surface=surface,
                )
            if (
                raw_context_protocol is not None
                and context_protocol is None
                and protocol is None
            ):
                _raise_collaboration_boundary_error(
                    event_context,
                    classification="unknown_state",
                    message="Collaboration protocol selection is unknown.",
                    surface=surface,
                )
        except _CollaborationBoundaryError as exc:
            _raise_collaboration_boundary_error(
                event_context,
                classification=exc.classification,
                message="Collaboration protocol boundary is malformed or ambiguous.",
                surface=surface,
                cause=exc,
            )

        if protocol is None:
            protocol = history_protocol

    if isinstance(event_context, dict) and protocol is not None:
        event_context["collaboration_protocol"] = protocol
    return protocol


def _is_collaboration_v2_context(event_context: Mapping[str, Any] | None) -> bool:
    return (event_context or {}).get("collaboration_protocol") == _COLLABORATION_V2


def _collaboration_context_with_protocol(
    event_context: Mapping[str, Any] | None,
    protocol: str | None,
) -> Mapping[str, Any] | None:
    if protocol is None or isinstance(event_context, dict):
        return event_context
    context = dict(event_context or {})
    context["collaboration_protocol"] = protocol
    return context


def _event_context_with_request_kind(context: Mapping[str, Any], request_kind: str) -> dict[str, Any]:
    payload = _public_event_context(context)
    existing = payload.get("request_kind")
    if isinstance(existing, str) and existing and existing != request_kind:
        payload.setdefault("client_request_kind", existing)
    payload["request_kind"] = request_kind
    return payload


def load_routing_config(path: Path = POLICY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    routing = data.get("routing", {})
    return routing if isinstance(routing, dict) else {}


def official_prefixes() -> tuple[str, ...]:
    prefixes = load_routing_config().get("official_prefixes", DEFAULT_OFFICIAL_PREFIXES)
    if not isinstance(prefixes, list):
        return DEFAULT_OFFICIAL_PREFIXES
    values = tuple(str(prefix) for prefix in prefixes if str(prefix))
    return values or DEFAULT_OFFICIAL_PREFIXES


def official_base_url() -> str:
    value = load_routing_config().get("official_upstream_base_url", OFFICIAL_BASE_URL)
    return str(value).rstrip("/") if value else OFFICIAL_BASE_URL


def ollama_cloud_base_url() -> str:
    value = load_routing_config().get("ollama_cloud_base_url", OLLAMA_CLOUD_BASE_URL)
    return str(value).rstrip("/") if value else OLLAMA_CLOUD_BASE_URL


def generated_catalog_slugs(path: Path = GENERATED_CATALOG_PATH) -> set[str]:
    return set(generated_catalog_by_slug(path))


def generated_catalog_by_slug(path: Path = GENERATED_CATALOG_PATH) -> dict[str, dict[str, Any]]:
    resolved_path = existing_generated_catalog_path(path)
    try:
        if resolved_path.exists():
            document = json.loads(resolved_path.read_text(encoding="utf-8-sig"))
            if not isinstance(document, Mapping) or not isinstance(document.get("models"), list):
                raise ValueError("catalog root must contain a models list")
        catalog_models = load_catalog_models(resolved_path)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ModelIdentityResolutionError(
            "published catalog is malformed and cannot authorize routing",
            classification="catalog_inconsistency",
            reason="malformed_catalog",
        ) from exc
    models: dict[str, dict[str, Any]] = {}
    if not isinstance(catalog_models, list):
        raise ModelIdentityResolutionError(
            "published catalog models must be a list",
            classification="catalog_inconsistency",
            reason="malformed_catalog",
        )
    for model in catalog_models:
        if not isinstance(model, Mapping):
            raise ModelIdentityResolutionError(
                "published catalog contains a non-object model row",
                classification="catalog_inconsistency",
                reason="malformed_model_row",
            )
        raw_slug_value = model.get("slug")
        if not isinstance(raw_slug_value, str) or not raw_slug_value.strip():
            raise ModelIdentityResolutionError(
                "published catalog contains a model without a slug",
                classification="catalog_inconsistency",
                reason="missing_catalog_slug",
            )
        raw_slug = canonical_model_id(raw_slug_value)
        slug = _catalog_identity_slug(raw_slug)
        if slug in models:
            raise ModelIdentityResolutionError(
                "published catalog contains duplicate canonical model identity",
                classification="catalog_inconsistency",
                reason="duplicate_canonical_slug",
                model_slug=slug,
            )
        models[slug] = dict(model)
    return models


def _catalog_identity_slug(slug: str) -> str:
    """Return the one route identity key used by the published catalog.

    ``openai/gpt-*`` is a provider-qualified spelling of the same exact
    Official model slug.  Every other provider namespace remains part of the
    key; no display name, case-folded alias, or nearest match is introduced.
    """

    value = canonical_model_id(slug)
    if value.startswith("openai/gpt-"):
        return value.removeprefix("openai/")
    return value


def _published_catalog_model(slug: str) -> dict[str, Any] | None:
    """Resolve one exact slug from the current generated catalog.

    The legacy catalog filename is intentionally not an identity authority.
    It may still be read by presentation-only compatibility code, but routing
    refuses to bind a request to it when the current publication is absent.
    """

    resolved_path = existing_generated_catalog_path(GENERATED_CATALOG_PATH)
    if resolved_path == LEGACY_GENERATED_CATALOG_PATH:
        raise ModelIdentityResolutionError(
            "current generated catalog is missing; legacy catalog cannot authorize routing",
            classification="catalog_inconsistency",
            reason="stale_legacy_catalog",
        )
    catalog = generated_catalog_by_slug(resolved_path)
    identity_slug = _catalog_identity_slug(slug)
    model = catalog.get(identity_slug)
    if model is None and identity_slug.startswith("gpt-"):
        # Compatibility with in-memory catalog fixtures that still expose the
        # pre-#273 provider-qualified key.  On-disk catalogs are normalized by
        # generated_catalog_by_slug before this branch can be reached.
        model = catalog.get(f"openai/{identity_slug}")
    return model


def _identity_failure(
    message: str,
    *,
    reason: str,
    provider_id: str | None = None,
    model_slug: str | None = None,
) -> ModelIdentityResolutionError:
    return ModelIdentityResolutionError(
        message,
        classification="local_resolution_failure",
        reason=reason,
        provider_id=provider_id,
        model_slug=model_slug,
    )


def _catalog_failure(
    message: str,
    *,
    reason: str,
    provider_id: str | None = None,
    model_slug: str | None = None,
) -> ModelIdentityResolutionError:
    return ModelIdentityResolutionError(
        message,
        classification="catalog_inconsistency",
        reason=reason,
        provider_id=provider_id,
        model_slug=model_slug,
    )


def _is_internal_route_identity(value: Any) -> bool:
    """Return whether any route/config identity reserves the internal reviewer slug."""

    values: list[Any]
    if isinstance(value, Mapping):
        if is_internal_model(value):
            return True
        values = [
            value.get(key)
            for key in (
                "id",
                "slug",
                "model",
                "name",
                "alias",
                "matched_alias",
                "model_id",
                "upstream_model",
            )
        ]
    else:
        values = [value]

    for raw_value in values:
        if not isinstance(raw_value, str):
            continue
        identity = canonical_model_id(raw_value).strip().lower()
        if identity and any(part.strip() == "codex-auto-review" for part in identity.split("/")):
            return True
    return False


def _safe_error_identity(value: Any) -> str | None:
    """Keep credential-like or malformed identities out of error payloads."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", candidate):
        return None
    lowered = candidate.lower()
    if any(marker in lowered for marker in ("bearer", "secret", "token", "password", "api_key", "api-key", "authorization", "cookie")):
        return None
    return candidate


def _validate_published_catalog_model_for_provider(
    model: Mapping[str, Any],
    *,
    provider_id: str,
    model_slug: str,
    expected_upstream_model: str | None = None,
) -> None:
    """Reject catalog rows that are internal, unsupported, or cross-provider."""

    if _is_internal_route_identity(model):
        raise _identity_failure(
            f"model identity is internal and cannot be routed: {model_slug}",
            reason="internal_model",
            provider_id=provider_id,
            model_slug=model_slug,
        )
    visibility = model_visibility(model, missing_is_list=True)
    if visibility is not CatalogVisibility.LIST:
        raise _catalog_failure(
            "published catalog model is not explicitly listable",
            reason="unsupported_visibility",
            provider_id=provider_id,
            model_slug=model_slug,
        )
    if "supported_in_api" in model and not isinstance(model["supported_in_api"], bool):
        raise _catalog_failure(
            "published catalog model supported_in_api is malformed",
            reason="malformed_supported_in_api",
            provider_id=provider_id,
            model_slug=model_slug,
        )
    if model.get("supported_in_api") is False:
        raise _identity_failure(
            f"model identity is not supported in the API: {model_slug}",
            reason="unsupported_model",
            provider_id=provider_id,
            model_slug=model_slug,
        )
    raw_slug = model.get("slug")
    if isinstance(raw_slug, str) and canonical_model_id(raw_slug) != canonical_model_id(model_slug):
        raise _catalog_failure(
            "published catalog row slug contradicts the requested model slug",
            reason="configured_model_mismatch",
            provider_id=provider_id,
            model_slug=model_slug,
        )
    metadata = model.get("codex_proxy_metadata")
    if "codex_proxy_metadata" in model and not isinstance(metadata, Mapping):
        raise _catalog_failure(
            "published catalog model metadata is malformed",
            reason="malformed_metadata",
            provider_id=provider_id,
            model_slug=model_slug,
        )
    if isinstance(metadata, Mapping):
        catalog_provider = canonical_model_id(str(metadata.get("provider") or ""))
        allowed_providers = {provider_id, provider_id.replace("-", "_")}
        if not catalog_provider or catalog_provider not in allowed_providers:
            raise _catalog_failure(
                f"model identity belongs to another provider: {model_slug}",
                reason="provider_mismatch",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        expected_upstream = canonical_model_id(expected_upstream_model or model_slug)
        catalog_upstream = metadata.get("upstream_model")
        if not isinstance(catalog_upstream, str) or canonical_model_id(catalog_upstream) != expected_upstream:
            raise _catalog_failure(
                "published catalog row upstream_model contradicts the requested model slug",
                reason="upstream_model_mismatch",
                provider_id=provider_id,
                model_slug=model_slug,
            )
        catalog_upstream_name = canonical_model_id(str(metadata.get("upstream_name") or ""))
        if catalog_upstream_name not in allowed_providers:
            raise _catalog_failure(
                "published catalog row upstream_name contradicts the provider identity",
                reason="upstream_name_mismatch",
                provider_id=provider_id,
                model_slug=model_slug,
            )


def _provider_catalog_failure(
    message: str,
    *,
    provider_id: str,
    model_slug: str,
) -> ModelIdentityResolutionError:
    """Wrap provider-index parse/collision failures as stable catalog errors."""

    return _catalog_failure(
        message,
        reason="provider_model_index_inconsistency",
        provider_id=provider_id,
        model_slug=model_slug,
    )


def _resolve_external_model_alias(slug: str) -> dict[str, Any] | None:
    """Resolve one configured external model without leaking raw config errors."""

    try:
        return resolve_external_model_alias(slug)
    except ModelIdentityResolutionError:
        raise
    except ValueError as exc:
        raise _provider_catalog_failure(
            "external provider model index is inconsistent",
            provider_id=slug.partition("/")[0] or "external",
            model_slug=slug,
        ) from exc


def _resolve_ollama_cloud_model(
    model_id: str,
    *,
    require_api_key: bool,
) -> tuple[bool, dict[str, Any] | None]:
    """Resolve Ollama Cloud configuration with catalog-level diagnostics."""

    try:
        return resolve_ollama_cloud_model(model_id, require_api_key=require_api_key)
    except ModelIdentityResolutionError:
        raise
    except ValueError as exc:
        raise _provider_catalog_failure(
            "Ollama Cloud model index is inconsistent",
            provider_id="ollama-cloud",
            model_slug=canonical_model_id(model_id),
        ) from exc


def _catalog_output_limit(model_id: str) -> tuple[int | None, bool]:
    slug = canonical_model_id(model_id)
    model = generated_catalog_by_slug().get(_catalog_identity_slug(slug))
    if not model:
        cap = UPSTREAM_MAX_OUTPUT_TOKEN_CAPS.get(slug)
        return (cap if isinstance(cap, int) and cap > 0 else None), False
    value = model.get("max_output_tokens")
    catalog_value = value if isinstance(value, int) and value > 0 else None
    cap = UPSTREAM_MAX_OUTPUT_TOKEN_CAPS.get(slug)
    if isinstance(cap, int) and cap > 0:
        return (min(catalog_value, cap) if catalog_value is not None else cap), False
    metadata = model.get("codex_proxy_metadata")
    context_fallback = (
        isinstance(metadata, Mapping)
        and metadata.get("max_output_source") == CONTEXT_WINDOW_OUTPUT_FALLBACK_SOURCE
    )
    return catalog_value, context_fallback


def catalog_max_output_tokens(model_id: str) -> int | None:
    return _catalog_output_limit(model_id)[0]


def policy_denies_model(model_id: Any, policy: Any) -> bool:
    slug = canonical_model_id(str(model_id))
    if not slug:
        return False
    if slug in policy.denied_models or deny_match_model_id(slug) in policy.denied_models:
        return True
    lowered = slug.lower()
    return any(part in lowered for part in policy.denied_substrings)


def policy_denies_any_model(model_ids: tuple[Any, ...], policy: Any) -> bool:
    return any(model_id is not None and policy_denies_model(model_id, policy) for model_id in model_ids)


def generated_official_catalog_upstream_model(slug: str, policy: Any) -> str | None:
    upstream_model = slug[len(OFFICIAL_ALIAS_PREFIX) :] if slug.startswith(OFFICIAL_ALIAS_PREFIX) else slug
    if not upstream_model.startswith(official_prefixes()):
        return None

    alias = f"{OFFICIAL_ALIAS_PREFIX}{upstream_model}"
    model = _published_catalog_model(upstream_model)
    if not model:
        return None
    if _is_internal_route_identity(model):
        raise _identity_failure(
            f"model identity is internal and cannot be routed: {slug}",
            reason="internal_model",
            provider_id="openai",
            model_slug=upstream_model,
        )
    if model_visibility(model, missing_is_list=True) is not CatalogVisibility.LIST:
        raise _catalog_failure(
            "published catalog model is not explicitly listable",
            reason="unsupported_visibility",
            provider_id="openai",
            model_slug=upstream_model,
        )
    if "supported_in_api" in model and not isinstance(model["supported_in_api"], bool):
        raise _catalog_failure(
            "official catalog model supported_in_api is malformed",
            reason="malformed_supported_in_api",
            provider_id="openai",
            model_slug=upstream_model,
        )
    if model.get("supported_in_api") is False:
        raise _identity_failure(
            f"model identity is not supported in the API: {slug}",
            reason="unsupported_model",
            provider_id="openai",
            model_slug=upstream_model,
        )

    metadata = model.get("codex_proxy_metadata")
    if "codex_proxy_metadata" in model and not isinstance(metadata, Mapping):
        raise _catalog_failure(
            "official catalog model metadata is malformed",
            reason="malformed_metadata",
            provider_id="openai",
            model_slug=upstream_model,
        )
    if not isinstance(metadata, Mapping):
        # The bundled policy is an authoritative identity for the legacy
        # gpt-5.5 route.  All newly discovered Official rows must carry the
        # generated metadata binding.
        if upstream_model == "gpt-5.5" and should_include_model(upstream_model, policy):
            return upstream_model
        raise _catalog_failure(
            "official catalog row is missing its upstream identity binding",
            reason="missing_catalog_metadata",
            provider_id="openai",
            model_slug=upstream_model,
        )
    catalog_upstream = canonical_model_id(str(metadata.get("upstream_model", "")))
    if (
        metadata.get("provider") != "openai"
        or metadata.get("upstream_name") != "official"
        or catalog_upstream != upstream_model
        or not catalog_upstream.startswith(official_prefixes())
    ):
        raise _catalog_failure(
            "official catalog row has contradictory upstream identity metadata",
            reason="upstream_model_mismatch",
            provider_id="openai",
            model_slug=upstream_model,
        )
    if policy_denies_any_model((slug, alias, catalog_upstream), policy):
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="denied_model",
            provider_id="openai",
            model_slug=upstream_model,
        )
    return catalog_upstream


def official_alias_upstream_model(slug: str, policy: Any) -> str | None:
    if not slug.startswith(OFFICIAL_ALIAS_PREFIX):
        return None
    upstream_model = slug[len(OFFICIAL_ALIAS_PREFIX) :]
    if policy_denies_any_model((slug, upstream_model), policy):
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="denied_model",
            provider_id="openai",
            model_slug=upstream_model,
        )
    if not upstream_model.startswith(official_prefixes()):
        return None
    # Provider qualification is presentation-neutral only after the exact
    # bare Official slug is proven by the current catalog.
    return generated_official_catalog_upstream_model(slug, policy)


def official_fast_variant_upstream_model(slug: str, policy: Any) -> str | None:
    fast_model = slug[len(OFFICIAL_ALIAS_PREFIX) :] if slug.startswith(OFFICIAL_ALIAS_PREFIX) else slug
    upstream_model = OFFICIAL_FAST_VARIANT_BASE_MODELS.get(fast_model)
    if upstream_model is None:
        return None
    upstream_alias = f"{OFFICIAL_ALIAS_PREFIX}{upstream_model}"
    if policy_denies_any_model((slug, fast_model, upstream_model, upstream_alias), policy):
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="denied_model",
            provider_id="openai",
            model_slug=fast_model,
        )
    if not upstream_model.startswith(official_prefixes()):
        return None
    resolved = generated_official_catalog_upstream_model(upstream_model, policy)
    if resolved is None:
        return None
    return resolved


OLLAMA_CLOUD_ALIAS_PREFIX = "ollama-cloud/"


def provider_scoped_path(path: str, endpoint_suffix: str) -> str | None:
    prefix = "/v1/providers/"
    suffix = "/" + endpoint_suffix.strip("/")
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    provider_part = path[len(prefix) : -len(suffix)]
    if not provider_part or "/" in provider_part:
        return None
    provider = unquote(provider_part).strip()
    if not provider:
        return None
    return provider


def provider_scoped_route_model(model_id: str | None, provider_hint: str | None) -> str | None:
    if not model_id:
        return None
    slug = canonical_model_id(str(model_id))
    if not slug or not provider_hint:
        return slug
    provider = canonical_model_id(str(provider_hint))
    if not provider:
        return slug
    if slug.startswith(f"{provider}/"):
        return slug
    return f"{provider}/{slug}"


def ollama_cloud_runtime_upstream(model_id: str, policy: Any) -> dict[str, Any] | None:
    configured, runtime_model = _resolve_ollama_cloud_model(
        model_id,
        require_api_key=False,
    )
    if not configured:
        return None
    slug = canonical_model_id(model_id)
    if runtime_model is None:
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="unsupported_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )
    if _is_internal_route_identity(slug) or _is_internal_route_identity(runtime_model):
        raise _identity_failure(
            f"model identity is internal and cannot be routed: {slug}",
            reason="internal_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )
    if runtime_model.get("matched_alias"):
        raise _identity_failure(
            "model aliases are presentation-only and cannot authorize routing",
            reason="model_alias_not_routable",
            provider_id="ollama-cloud",
            model_slug=slug,
        )

    policy_alias = runtime_model.get("alias", f"{OLLAMA_CLOUD_ALIAS_PREFIX}{slug}")
    upstream_model = runtime_model.get("upstream_model")
    if not isinstance(upstream_model, str) or not upstream_model.strip():
        raise _catalog_failure(
            "Ollama Cloud configuration is missing upstream_model",
            reason="missing_upstream_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )
    upstream_model = upstream_model.strip()
    configured_id = canonical_model_id(str(runtime_model.get("model_id") or slug))
    if configured_id != slug and configured_id != f"{OLLAMA_CLOUD_ALIAS_PREFIX}{slug}":
        raise _catalog_failure(
            "Ollama Cloud configuration model identity does not match the request",
            reason="configured_model_mismatch",
            provider_id="ollama-cloud",
            model_slug=slug,
        )
    if policy_denies_any_model((slug, policy_alias, upstream_model), policy):
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="denied_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )

    api_key = runtime_model.get("api_key")
    upstream: dict[str, Any] = {
        "name": "ollama_cloud",
        "provider_id": "ollama-cloud",
        "model_id": slug,
        "base_url": runtime_model.get("base_url") or ollama_cloud_base_url(),
        "auth": "api_key" if api_key else "ollama_api_key",
        "upstream_model": upstream_model,
        "upstream_format": runtime_model.get("upstream_format", "responses"),
        "tool_protocol": runtime_model.get("tool_protocol", "auto"),
        "tool_surface_strategy": runtime_model.get("tool_surface_strategy", "eager"),
        "native_responses_tool_codec": runtime_model.get(
            "native_responses_tool_codec", "none"
        ),
        "reports_cached_input_tokens": False,
        "input_modalities": tuple(runtime_model.get("input_modalities") or ("text",)),
        **_route_capability_metadata(runtime_model),
    }
    if api_key:
        upstream["api_key"] = api_key
    return upstream


def ollama_cloud_alias_upstream_model(slug: str, policy: Any) -> dict[str, Any] | None:
    if not slug.startswith(OLLAMA_CLOUD_ALIAS_PREFIX):
        return None
    upstream_model = slug[len(OLLAMA_CLOUD_ALIAS_PREFIX) :]
    if not upstream_model:
        return None
    if policy_denies_any_model((slug, upstream_model), policy):
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="denied_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )

    runtime_upstream = ollama_cloud_runtime_upstream(slug, policy)
    if runtime_upstream is not None:
        return runtime_upstream

    if not (should_include_model(slug, policy) or should_include_model(upstream_model, policy)):
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="unsupported_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )
    model = _published_catalog_model(slug)
    if model is None:
        raise _identity_failure(
            f"model is not in the generated cloud catalog: {upstream_model}",
            reason="missing_catalog_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )
    _validate_published_catalog_model_for_provider(
        model,
        provider_id="ollama-cloud",
        model_slug=slug,
        expected_upstream_model=upstream_model,
    )
    return {
        "name": "ollama_cloud",
        "provider_id": "ollama-cloud",
        "model_id": slug,
        "base_url": ollama_cloud_base_url(),
        "auth": "ollama_api_key",
        "upstream_model": upstream_model,
        "reports_cached_input_tokens": False,
    }


def _route_capability_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "tool_protocol_capabilities",
        "tool_exposure_mode",
        "tool_capability_state",
        "supports_search_tool",
        "proven_tool_subset",
        "capability_manifest_version",
        "capability_manifest_hash",
        "capability_manifest_state",
        "capability_binding",
    )
    return {key: source[key] for key in keys if key in source}


def choose_upstream(model_id: str) -> dict[str, Any]:
    slug = canonical_model_id(str(model_id))
    if not slug:
        raise ValueError("model is required")

    policy = load_policy(POLICY_PATH)
    official_fast_variant = official_fast_variant_upstream_model(slug, policy)
    if official_fast_variant is not None:
        return {
            "name": "official",
            "provider_id": "openai",
            "model_id": slug,
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "upstream_model": official_fast_variant,
            "service_tier": OFFICIAL_FAST_VARIANT_SERVICE_TIER,
            "reports_cached_input_tokens": True,
        }

    official_alias = official_alias_upstream_model(slug, policy)
    if official_alias is not None:
        return {
            "name": "official",
            "provider_id": "openai",
            "model_id": slug,
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "upstream_model": official_alias,
            "reports_cached_input_tokens": True,
        }

    discovered_official = generated_official_catalog_upstream_model(slug, policy)
    if discovered_official is not None:
        return {
            "name": "official",
            "provider_id": "openai",
            "model_id": slug,
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "upstream_model": discovered_official,
            "reports_cached_input_tokens": True,
        }

    ollama_alias = ollama_cloud_alias_upstream_model(slug, policy)
    if ollama_alias is not None:
        return ollama_alias

    if slug.startswith(official_prefixes()):
        raise _identity_failure(
            f"model is not in the generated Official catalog: {slug}",
            reason="missing_catalog_model",
            provider_id="openai",
            model_slug=slug,
        )

    external_model = _resolve_external_model_alias(slug)
    if external_model is not None:
        if _is_internal_route_identity(slug) or _is_internal_route_identity(external_model):
            raise _identity_failure(
                f"model identity is internal and cannot be routed: {slug}",
                reason="internal_model",
                provider_id=str(external_model.get("provider_alias") or "") or None,
                model_slug=slug,
            )
        policy_alias = external_model.get("alias", slug)
        if policy_denies_any_model((slug, policy_alias, external_model.get("matched_alias")), policy):
            raise _identity_failure(
                f"model is not allowed: {slug}",
                reason="denied_model",
                provider_id=str(external_model.get("provider_alias") or "") or None,
                model_slug=slug,
            )
        if not should_include_external_provider_model(policy_alias, policy):
            raise _identity_failure(
                f"model is not allowed: {slug}",
                reason="unsupported_model",
                provider_id=str(external_model.get("provider_alias") or "") or None,
                model_slug=slug,
            )
        provider_id = canonical_model_id(str(external_model.get("provider_alias") or ""))
        configured_model = canonical_model_id(str(external_model.get("alias") or ""))
        upstream_model_value = external_model.get("upstream_model")
        if not provider_id or not configured_model or not isinstance(upstream_model_value, str) or not upstream_model_value.strip():
            raise _catalog_failure(
                "external provider configuration is missing exact model identity",
                reason="missing_upstream_model",
                provider_id=provider_id or None,
                model_slug=slug,
            )
        if configured_model != slug:
            raise _identity_failure(
                "model aliases are presentation-only and cannot authorize routing",
                reason="model_alias_not_routable",
                provider_id=provider_id,
                model_slug=slug,
            )
        if canonical_model_id(upstream_model_value).lower() == "codex-auto-review":
            raise _identity_failure(
                f"model identity is internal and cannot be routed: {slug}",
                reason="internal_model",
                provider_id=provider_id,
                model_slug=slug,
            )
        return {
            "name": external_model["upstream_name"],
            "provider_id": provider_id,
            "model_id": slug,
            "base_url": external_model["base_url"],
            "auth": "api_key",
            "api_key": external_model["api_key"],
            "upstream_model": external_model["upstream_model"],
            "upstream_format": external_model.get("upstream_format", "responses"),
            "tool_protocol": external_model.get("tool_protocol", "auto"),
            "tool_surface_strategy": external_model.get("tool_surface_strategy", "eager"),
            "native_responses_tool_codec": external_model.get(
                "native_responses_tool_codec", "none"
            ),
            "reports_cached_input_tokens": bool(external_model.get("reports_cached_input_tokens")),
            "supports_developer_role": bool(external_model.get("supports_developer_role", True)),
            "supported_reasoning_levels": tuple(external_model.get("supported_reasoning_levels") or ()),
            "input_modalities": tuple(external_model.get("input_modalities") or ("text",)),
            **_route_capability_metadata(external_model),
        }

    if "/" in slug:
        raise _identity_failure(
            f"external provider model is not configured: {slug}",
            reason="unsupported_model",
            provider_id=slug.partition("/")[0],
            model_slug=slug,
        )

    runtime_ollama = ollama_cloud_runtime_upstream(slug, policy)
    if runtime_ollama is not None:
        return runtime_ollama

    if not should_include_model(slug, policy):
        raise _identity_failure(
            f"model is not allowed: {slug}",
            reason="unsupported_model",
            provider_id="ollama-cloud",
            model_slug=slug,
        )

    model = _published_catalog_model(slug)
    if model is not None:
        _validate_published_catalog_model_for_provider(
            model,
            provider_id="ollama-cloud",
            model_slug=slug,
            expected_upstream_model=slug,
        )
        return {
            "name": "ollama_cloud",
            "provider_id": "ollama-cloud",
            "model_id": slug,
            "base_url": ollama_cloud_base_url(),
            "auth": "ollama_api_key",
            "reports_cached_input_tokens": False,
            "upstream_model": slug,
        }

    raise _identity_failure(
        f"model is not in the generated cloud catalog: {slug}",
        reason="missing_catalog_model",
        provider_id="ollama-cloud",
        model_slug=slug,
    )


def official_upstream() -> dict[str, Any]:
    return {
        "name": "official",
        "provider_id": "openai",
        "base_url": official_base_url(),
        "auth": "codex_auth",
        "reports_cached_input_tokens": True,
    }


def _reasoning_param_is_unsupported(upstream_name: Any, requested_model: Any, upstream_model: Any) -> bool:
    if upstream_name == "official":
        return False
    for model in (upstream_model, requested_model):
        if not isinstance(model, str) or not model:
            continue
        model_key = canonical_model_id(model).lower()
        if any(model_key.startswith(prefix) for prefix in UNSUPPORTED_REASONING_MODEL_PREFIXES):
            return True
    return False


def _request_carries_reasoning_control(payload: Mapping[str, Any]) -> bool:
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        return True
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        return True
    if isinstance(reasoning, Mapping) and reasoning:
        return True
    template_kwargs = payload.get("chat_template_kwargs")
    if isinstance(template_kwargs, Mapping):
        template_effort = template_kwargs.get("reasoning_effort")
        if isinstance(template_effort, str) and template_effort:
            return True
    return False


def _reasoning_policy_for_request(
    inbound_payload: Any,
    upstream: Mapping[str, Any] | None,
    model: str | None,
) -> str | None:
    if not isinstance(inbound_payload, Mapping) or not isinstance(upstream, Mapping):
        return None
    if _request_carries_reasoning_control(inbound_payload):
        return "explicit"
    levels = upstream.get("supported_reasoning_levels")
    if not levels and model:
        candidate = generated_catalog_by_slug().get(
            _catalog_identity_slug(canonical_model_id(model))
        )
        if isinstance(candidate, Mapping):
            levels = candidate.get("supported_reasoning_levels")
    if levels:
        return "provider-default"
    return None


def _validate_reasoning_effort_for_upstream(
    payload: Any,
    upstream: Mapping[str, Any],
    model: str | None,
) -> None:
    if not isinstance(payload, Mapping):
        return
    requested_efforts = [payload.get("reasoning_effort")]
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, Mapping):
        requested_efforts.append(reasoning.get("effort"))
    elif isinstance(reasoning, str):
        requested_efforts.append(reasoning)
    template_kwargs = payload.get("chat_template_kwargs")
    if isinstance(template_kwargs, Mapping):
        requested_efforts.append(template_kwargs.get("reasoning_effort"))
    is_ultra = any(
        isinstance(effort, str) and effort.strip().lower() == "ultra" for effort in requested_efforts
    )
    if not is_ultra:
        return
    is_official = upstream.get("name") == "official" and upstream.get("auth") == "codex_auth"
    model_id = canonical_model_id(model or "").lower()
    if model_id.startswith(OFFICIAL_ALIAS_PREFIX):
        model_id = model_id[len(OFFICIAL_ALIAS_PREFIX) :]
    if is_official and model_id in OFFICIAL_ULTRA_REASONING_MODELS:
        return
    if is_official:
        raise ValueError(
            "reasoning effort 'ultra' is supported only for gpt-5.6-sol and gpt-5.6-terra"
        )
    raise ValueError("reasoning effort 'ultra' is not supported for third-party models")


def decoded_request_body(body: bytes, content_encoding: str | None = None) -> tuple[bytes, bool, str | None]:
    if not content_encoding:
        return body, False, None
    encoding = content_encoding.lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(body), True, None
        if "deflate" in encoding:
            return zlib.decompress(body), True, None
        if "zstd" in encoding:
            if zstandard is None:
                return body, False, "zstandard module is not available"
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)) as reader:
                return reader.read(), True, None
    except DECODE_ERRORS as exc:
        return body, False, f"{type(exc).__name__}: {exc}"
    return body, False, None


def _decode_json_string_token(token: bytes) -> str | None:
    try:
        value = json.loads(token.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) and value.strip() else None


def try_extract_model(body: bytes, content_encoding: str | None = None) -> str | None:
    scan_body, _, _ = decoded_request_body(body, content_encoding)
    try:
        payload = json.loads(scan_body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None

    if isinstance(payload, dict):
        model = payload.get("model")
        return model if isinstance(model, str) and model.strip() else None

    form_match = FORM_MODEL_RE.search(scan_body)
    if form_match:
        try:
            form_model = form_match.group(1).strip().decode("utf-8")
        except UnicodeDecodeError:
            form_model = ""
        if form_model:
            return form_model

    for match in EMBEDDED_MODEL_RE.finditer(scan_body):
        token = match.group(0).split(b":", 1)[1].strip()
        model = _decode_json_string_token(token)
        if model:
            return model
    return None


def extract_model(body: bytes) -> str:
    model = try_extract_model(body)
    if model:
        return model

    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must include a string model") from exc
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    raise ValueError("request body must include a string model")


def _looks_like_official_encrypted_content(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(OFFICIAL_ENCRYPTED_CONTENT_PREFIX)


def _sanitize_official_reasoning_items(value: Any) -> bool:
    changed = False

    if isinstance(value, list):
        for item in value:
            if _sanitize_official_reasoning_items(item):
                changed = True
        return changed

    if not isinstance(value, dict):
        return False

    if value.get("type") == "reasoning" and "encrypted_content" in value:
        if not _looks_like_official_encrypted_content(value.get("encrypted_content")):
            value.pop("encrypted_content", None)
            changed = True

    for item in value.values():
        if _sanitize_official_reasoning_items(item):
            changed = True

    return changed


def _sanitize_official_input_reasoning_items(payload: dict[str, Any]) -> tuple[bool, dict[str, int]]:
    """Remove non-portable reasoning references at the Official input boundary.

    Codex stores third-party Responses reasoning items in the shared task
    history.  An Official ``store=false`` request cannot resolve those
    provider-local IDs, so forwarding the whole item turns a model switch into
    a permanent 404/reconnect loop.  Only a self-contained Official encrypted
    item is portable; every other reasoning item is dropped as a whole.  The
    walk is deliberately limited to input containers so response metadata,
    tool schemas, and ordinary transcript fields remain untouched.
    """

    counts = {
        "removed_non_portable": 0,
        "kept_official_encrypted": 0,
    }

    def sanitize_input_list(items: list[Any]) -> tuple[list[Any], bool]:
        changed = False
        rewritten: list[Any] = []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                encrypted_content = item.get("encrypted_content")
                if _looks_like_official_encrypted_content(encrypted_content):
                    counts["kept_official_encrypted"] += 1
                    rewritten.append(item)
                else:
                    counts["removed_non_portable"] += 1
                    changed = True
                continue

            if isinstance(item, list):
                nested_items, nested_changed = sanitize_input_list(item)
                if nested_changed:
                    item = nested_items
                    changed = True
            elif isinstance(item, dict) and isinstance(item.get("input"), list):
                nested_items, nested_changed = sanitize_input_list(item["input"])
                if nested_changed:
                    item = {**item, "input": nested_items}
                    changed = True
            rewritten.append(item)
        return rewritten, changed

    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False, counts

    rewritten_items, changed = sanitize_input_list(input_items)
    if changed:
        payload["input"] = rewritten_items
    return changed, counts


def _strip_reasoning_encrypted_content(value: Any) -> bool:
    changed = False

    if isinstance(value, list):
        for item in value:
            if _strip_reasoning_encrypted_content(item):
                changed = True
        return changed

    if not isinstance(value, dict):
        return False

    if value.get("type") == "reasoning" and "encrypted_content" in value:
        value.pop("encrypted_content", None)
        changed = True

    for item in value.values():
        if _strip_reasoning_encrypted_content(item):
            changed = True

    return changed


RAW_REASONING_DELTA_EVENTS = {
    "response.reasoning_text.delta",
    "response.reasoning_content.delta",
    "response.reasoning_raw_content.delta",
}
REASONING_TEXT_EVENT_PREFIXES = (
    "response.reasoning_text.",
    "response.reasoning_content.",
    "response.reasoning_raw_content.",
)
REASONING_SUMMARY_EVENT_PREFIX = "response.reasoning_summary_text."


def _collect_text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []

    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_collect_text_fragments(item))
        return fragments

    if isinstance(value, dict):
        fragments: list[str] = []
        for key in ("text", "content", "summary", "message"):
            if key in value:
                fragments.extend(_collect_text_fragments(value[key]))
        return fragments

    return []


def _has_browser_context_signal(value: Any) -> bool:
    for fragment in _collect_text_fragments(value):
        lowered = fragment.lower()
        if any(marker in lowered for marker in BROWSER_CONTEXT_MARKERS):
            return True
        if BROWSER_CURRENT_URL_RE.search(fragment):
            return True
    return False


def _has_browser_context_guidance(value: Any) -> bool:
    return any(BROWSER_CONTEXT_GUIDANCE_SENTINEL in fragment for fragment in _collect_text_fragments(value))


def _user_text_message(content: str) -> dict[str, str]:
    return {"type": "message", "role": "user", "content": content}


def _inject_browser_context_guidance(
    payload: dict[str, Any],
    *,
    upstream_name: Any,
    event_context: Mapping[str, Any] | None = None,
) -> bool:
    input_items = payload.get("input")
    if not _has_browser_context_signal(input_items) or _has_browser_context_guidance(input_items):
        return False

    guidance_message = _developer_text_message(BROWSER_CONTEXT_GUIDANCE)

    if isinstance(input_items, list):
        input_items.append(guidance_message)
    elif isinstance(input_items, str):
        payload["input"] = [_user_text_message(input_items), guidance_message]
    else:
        return False

    _write_adapter_event(
        event_context,
        "browser_context_guidance_injected",
        upstream=upstream_name if isinstance(upstream_name, str) else None,
        model=payload.get("model") if isinstance(payload.get("model"), str) else None,
    )
    return True


def _hide_reasoning_text(value: Any) -> bool:
    changed = False

    if isinstance(value, list):
        for item in value:
            if _hide_reasoning_text(item):
                changed = True
        return changed

    if not isinstance(value, dict):
        return False

    if value.get("type") == "reasoning":
        summary = value.get("summary")
        valid_summary = (
            [
                {"type": "summary_text", "text": item["text"]}
                for item in summary
                if isinstance(item, dict)
                and item.get("type") == "summary_text"
                and isinstance(item.get("text"), str)
            ]
            if isinstance(summary, list)
            else []
        )
        if summary != valid_summary:
            value["summary"] = valid_summary
            changed = True
        for key in ("content", "raw_content", "reasoning_content", "thinking", "encrypted_content"):
            if key in value:
                value.pop(key, None)
                changed = True

    for item in value.values():
        if _hide_reasoning_text(item):
            changed = True

    return changed


def _is_raw_reasoning_stream_event(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get("type")
    return isinstance(event_type, str) and event_type.startswith(REASONING_TEXT_EVENT_PREFIXES)


def _is_reasoning_summary_stream_event(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get("type")
    return isinstance(event_type, str) and event_type.startswith(REASONING_SUMMARY_EVENT_PREFIX)


def _is_reasoning_text_stream_event(payload: Mapping[str, Any]) -> bool:
    return _is_raw_reasoning_stream_event(payload) or _is_reasoning_summary_stream_event(payload)


def _sse_line_ending(line: bytes) -> bytes:
    for candidate in (b"\r\n", b"\n", b"\r"):
        if line.endswith(candidate):
            return candidate
    return b"\n"


def _sse_event_separator_after_line(line: bytes) -> bytes:
    if line.endswith((b"\r\n\r\n", b"\n\n", b"\r\r")):
        return b""
    line_ending = _sse_line_ending(line)
    if line.endswith(line_ending):
        return line_ending
    return line_ending + line_ending


def _is_sse_blank_line(line: bytes) -> bool:
    return line in {b"\n", b"\r\n", b"\r"}


def _is_sse_event_metadata_line(line: bytes) -> bool:
    return line.startswith((b"event:", b"id:", b"retry:"))


def _sse_payload_bytes(line: bytes) -> bytes | None:
    if not line.startswith(b"data:"):
        return None

    content = line
    for candidate in (b"\r\n", b"\n", b"\r"):
        if line.endswith(candidate):
            content = line[: -len(candidate)]
            break

    payload_bytes = content[5:].lstrip()
    if not payload_bytes:
        return None
    return payload_bytes


def _parse_sse_json_payload(line: bytes) -> dict[str, Any] | None:
    payload_bytes = _sse_payload_bytes(line)
    if payload_bytes is None:
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_sse_json_payloads(blob: bytes) -> list[dict[str, Any]]:
    """Parse every JSON data frame emitted for one upstream SSE line.

    Runtime compatibility adapters can buffer one upstream function lifecycle
    and emit several native Responses events as a single byte string.  Relay
    bookkeeping must inspect each emitted frame rather than treating that
    byte string as one JSON payload.
    """

    return [
        payload
        for line in blob.splitlines(keepends=True)
        for payload in [_parse_sse_json_payload(line)]
        if payload is not None
    ]


class UpstreamSseSemanticError(ValueError):
    """A complete converted SSE frame is not valid source-protocol JSON."""


def _verified_converted_sse_semantic_error(
    source_format: str,
) -> UpstreamSseSemanticError:
    source_label = (
        "Responses" if source_format == "responses" else "Chat Completions"
    )
    return UpstreamSseSemanticError(
        f"Upstream returned a structurally invalid complete {source_label} SSE event."
    )


def _validate_verified_converted_sse_payload(
    payload: Mapping[str, Any],
    source_format: str,
) -> None:
    def invalid_shape() -> None:
        raise _verified_converted_sse_semantic_error(source_format)

    def validate_usage(
        usage: Any,
        *,
        token_fields: tuple[str, ...],
        detail_fields: tuple[str, ...],
    ) -> None:
        if not isinstance(usage, Mapping):
            invalid_shape()
        for field in token_fields:
            if field in usage and type(usage.get(field)) is not int:
                invalid_shape()
        for field in detail_fields:
            if field not in usage:
                continue
            details = usage.get(field)
            if not isinstance(details, Mapping):
                invalid_shape()
            if any(type(count) is not int for count in details.values()):
                invalid_shape()

    def validate_error_envelope(error: Any) -> None:
        if isinstance(error, str):
            if not error:
                invalid_shape()
            return
        if not isinstance(error, Mapping):
            invalid_shape()
        message = error.get("message")
        if not isinstance(message, str) or not message:
            invalid_shape()
        if "code" in error:
            code = error.get("code")
            if code is not None and type(code) not in {str, int}:
                invalid_shape()

    if source_format == "responses":
        def validate_content_part(part: Any) -> None:
            if not isinstance(part, Mapping):
                invalid_shape()
            part_type = part.get("type")
            if not isinstance(part_type, str):
                invalid_shape()
            if part_type in {"input_text", "output_text", "text"}:
                if not isinstance(part.get("text"), str):
                    invalid_shape()
                if (
                    "annotations" in part
                    and not isinstance(part.get("annotations"), list)
                ):
                    invalid_shape()
            elif part_type == "input_image":
                if not isinstance(part.get("image_url"), str):
                    invalid_shape()
                if (
                    "detail" in part
                    and not isinstance(part.get("detail"), str)
                ):
                    invalid_shape()

        def validate_output_item(item: Any) -> None:
            if not isinstance(item, Mapping):
                invalid_shape()
            if not isinstance(item.get("type"), str):
                invalid_shape()
            for field in ("id", "status"):
                if field in item and not isinstance(item.get(field), str):
                    invalid_shape()
            if item.get("type") == "message":
                if "role" in item and not isinstance(item.get("role"), str):
                    invalid_shape()
                if "content" in item:
                    content = item.get("content")
                    if not isinstance(content, list):
                        invalid_shape()
                    for part in content:
                        validate_content_part(part)
            if item.get("type") == "function_call":
                for field in ("call_id", "namespace", "name", "arguments"):
                    if field in item and not isinstance(item.get(field), str):
                        invalid_shape()

        event_type = payload.get("type")
        if not isinstance(event_type, str):
            invalid_shape()
        if event_type in {
            "response.created",
            "response.completed",
            "response.incomplete",
            "response.failed",
        }:
            response = payload.get("response")
            if not isinstance(response, Mapping):
                invalid_shape()
            for field in ("id", "model", "status"):
                if field in response and not isinstance(response.get(field), str):
                    invalid_shape()
            if "output" in response:
                output = response.get("output")
                if not isinstance(output, list):
                    invalid_shape()
                for item in output:
                    validate_output_item(item)
            if "usage" in response:
                validate_usage(
                    response.get("usage"),
                    token_fields=("input_tokens", "output_tokens", "total_tokens"),
                    detail_fields=(
                        "input_tokens_details",
                        "output_tokens_details",
                    ),
                )
            if event_type == "response.failed":
                if "error" not in response:
                    invalid_shape()
                validate_error_envelope(response.get("error"))
        elif event_type == "error":
            if "error" not in payload:
                invalid_shape()
            validate_error_envelope(payload.get("error"))
        elif event_type == "response.output_text.delta":
            if not isinstance(payload.get("delta"), str):
                invalid_shape()
        elif event_type in {
            "response.content_part.added",
            "response.content_part.done",
        }:
            validate_content_part(payload.get("part"))
        elif event_type in {
            "response.output_item.added",
            "response.output_item.done",
        }:
            validate_output_item(payload.get("item"))
        elif event_type == "response.function_call_arguments.delta":
            if (
                not isinstance(payload.get("item_id"), str)
                or not isinstance(payload.get("delta"), str)
            ):
                invalid_shape()
        elif event_type == "response.function_call_arguments.done":
            if (
                not isinstance(payload.get("item_id"), str)
                or not isinstance(payload.get("arguments"), str)
            ):
                invalid_shape()
        return
    if source_format != "chat_completions":
        return
    choices = payload.get("choices")
    if "error" in payload:
        validate_error_envelope(payload.get("error"))
        if choices is None:
            return
    if not isinstance(choices, list):
        invalid_shape()
    if "usage" in payload:
        validate_usage(
            payload.get("usage"),
            token_fields=("prompt_tokens", "completion_tokens", "total_tokens"),
            detail_fields=(
                "prompt_tokens_details",
                "completion_tokens_details",
            ),
        )
    for field in ("id", "object", "model"):
        if field in payload and not isinstance(payload.get(field), str):
            invalid_shape()
    for choice in choices:
        if not isinstance(choice, Mapping):
            invalid_shape()
        if "index" in choice and type(choice.get("index")) is not int:
            invalid_shape()
        if "delta" in choice and not isinstance(choice.get("delta"), Mapping):
            invalid_shape()
        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                invalid_shape()
            tool_calls = delta.get("tool_calls")
            if tool_calls is not None:
                if not isinstance(tool_calls, list):
                    invalid_shape()
                for tool_call in tool_calls:
                    if not isinstance(tool_call, Mapping):
                        invalid_shape()
                    if (
                        "index" in tool_call
                        and type(tool_call.get("index")) is not int
                    ):
                        invalid_shape()
                    for field in ("id", "type"):
                        if (
                            field in tool_call
                            and not isinstance(tool_call.get(field), str)
                        ):
                            invalid_shape()
                    function = tool_call.get("function")
                    if function is not None:
                        if not isinstance(function, Mapping):
                            invalid_shape()
                        for field in ("name", "arguments"):
                            if (
                                field in function
                                and not isinstance(function.get(field), str)
                            ):
                                invalid_shape()
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            invalid_shape()


def _converted_sse_payload(
    event: SseEvent,
    *,
    verified_source_format: str | None = None,
) -> Mapping[str, Any] | str | None:
    if not any(line.name == b"data" for line in event.lines) or not event.data:
        return None
    if event.data == b"[DONE]":
        return "[DONE]"
    try:
        payload = json.loads(event.data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamSseSemanticError(
            "Upstream returned a malformed complete SSE event."
        ) from exc
    if not isinstance(payload, Mapping):
        raise UpstreamSseSemanticError(
            "Upstream returned a structurally invalid complete SSE event."
        )
    if verified_source_format is not None:
        _validate_verified_converted_sse_payload(payload, verified_source_format)
    return payload


def _responses_sse_event_resets_idle_timeout(event: SseEvent) -> bool:
    try:
        payload = _converted_sse_payload(event)
    except UpstreamSseSemanticError:
        return False
    if not isinstance(payload, Mapping):
        return False
    event_type = payload.get("type")
    return isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error")


def _chat_sse_event_resets_idle_timeout(event: SseEvent) -> bool:
    try:
        payload = _converted_sse_payload(event)
    except UpstreamSseSemanticError:
        return False
    return payload == "[DONE]" or isinstance(payload, Mapping)


SSE_EVENT_TYPE_TELEMETRY_LIMIT = 64


class PassthroughSseSemanticStats:
    def __init__(
        self,
        *,
        terminal_observer: Callable[[str | None, bytes, Any], bool] | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events_streamed = 0
        self.json_events_streamed = 0
        self.terminal_event_seen = False
        self.completed_event_seen = False
        self.done_sentinel_seen = False
        self.response_event_seen = False
        self.downstream_output_seen = False
        self.last_event_type: str | None = None
        self.response_id: str | None = None
        self.event_type_counts: dict[str, int] = {}
        self.event_types_truncated = False
        self._terminal_observer = (
            terminal_observer if terminal_observer is not None else _responses_terminal_observer
        )
        self._clock = clock
        self._started_at = self._clock()
        self._first_byte_elapsed_ms: int | None = None
        self._first_event_elapsed_ms: int | None = None
        self._last_event_at: float | None = None
        self._max_inter_event_gap_ms: int | None = None
        self._last_inter_event_gap_ms: int | None = None
        self._terminal_elapsed_ms: int | None = None
        self._assembler = SseEventAssembler(max_frame_bytes=max_frame_bytes)
        self._eof_disposition: str | None = None
        self._incomplete_bytes_discarded = 0

    def observe_bytes(self, chunk: bytes) -> None:
        if self._eof_disposition == "size_limit":
            return
        if chunk and self._first_byte_elapsed_ms is None:
            self._first_byte_elapsed_ms = self._elapsed_ms(self._clock() - self._started_at)
        try:
            self._assembler.feed(chunk, on_event=self._observe_event)
        except SseFrameTooLargeError:
            self._eof_disposition = "size_limit"
            raise

    def finalize_pending(self) -> None:
        if self._eof_disposition is not None:
            return
        termination = self._assembler.finish()
        for event in termination.events:
            self._observe_event(event)
        self._eof_disposition = termination.disposition
        self._incomplete_bytes_discarded = termination.discarded_bytes

    def pending_completion_bytes(self) -> bytes:
        if self._eof_disposition == "size_limit":
            return b""
        return self._assembler.completion_bytes()

    def fields(self) -> dict[str, Any]:
        event_types = sorted(self.event_type_counts)
        fields: dict[str, Any] = {
            "sse_events_streamed": self.events_streamed,
            "sse_json_events_streamed": self.json_events_streamed,
            "sse_terminal_event_seen": self.terminal_event_seen,
            "sse_completed_event_seen": self.completed_event_seen,
            "sse_done_sentinel_seen": self.done_sentinel_seen,
            "sse_response_event_seen": self.response_event_seen,
            "sse_downstream_output_seen": self.downstream_output_seen,
            "sse_event_types": event_types,
            "sse_event_type_counts": {key: self.event_type_counts[key] for key in event_types},
            # The Gateway observes the stream after the response has already
            # been opened.  Successful DNS/TCP/TLS phase duration is therefore
            # intentionally explicit as not observed here; transport failures
            # carry their concrete phase from the upstream-open boundary.
            "upstream_connect_timing": "not_observed",
            "upstream_tls_timing": "not_observed",
            "sse_first_byte_elapsed_ms": self._first_byte_elapsed_ms,
            "sse_first_event_elapsed_ms": self._first_event_elapsed_ms,
            "sse_last_inter_event_gap_ms": self._last_inter_event_gap_ms,
            "sse_max_inter_event_gap_ms": self._max_inter_event_gap_ms,
            "sse_terminal_elapsed_ms": self._terminal_elapsed_ms,
        }
        if self.last_event_type is not None:
            fields["sse_last_event_type"] = self.last_event_type
        if self.event_types_truncated:
            fields["sse_event_types_truncated"] = True
        if self._eof_disposition == "incomplete":
            fields["sse_eof_disposition"] = "incomplete"
            fields["sse_incomplete_bytes_discarded"] = self._incomplete_bytes_discarded
        return fields

    def _observe_event(self, event: SseEvent) -> None:
        observed_at = self._clock()
        if self._first_event_elapsed_ms is None:
            self._first_event_elapsed_ms = self._elapsed_ms(observed_at - self._started_at)
        if self._last_event_at is not None:
            gap_ms = self._elapsed_ms(observed_at - self._last_event_at)
            self._last_inter_event_gap_ms = gap_ms
            if self._max_inter_event_gap_ms is None or gap_ms > self._max_inter_event_gap_ms:
                self._max_inter_event_gap_ms = gap_ms
        self._last_event_at = observed_at
        event_name: str | None = None
        if event.event is not None:
            event_name = event.event.decode("utf-8", errors="replace").strip() or None
        has_data_field = any(line.name == b"data" for line in event.lines)
        if event_name is None and not has_data_field:
            return
        data = event.data

        self.events_streamed += 1
        event_type = event_name
        payload: Any = None
        if data == b"[DONE]":
            self.done_sentinel_seen = True
            event_type = event_type or "[DONE]"
        elif data:
            try:
                payload = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, Mapping):
                self.json_events_streamed += 1
                payload_type = payload.get("type")
                if isinstance(payload_type, str) and payload_type:
                    event_type = payload_type
                if self._terminal_observer is _responses_terminal_observer:
                    if _responses_event_commits_downstream_output(payload, "official"):
                        self.downstream_output_seen = True
                    response = payload.get("response")
                    if isinstance(response, Mapping):
                        response_id = response.get("id")
                        if isinstance(response_id, str) and response_id:
                            self.response_id = response_id

        if event_type is None:
            return
        self.last_event_type = event_type
        self._record_event_type(event_type)
        if self._terminal_observer is _responses_terminal_observer:
            if event_type.startswith("response."):
                self.response_event_seen = True
            if event_type == "response.completed":
                self.completed_event_seen = True
        if self._terminal_observer(event_name, data, payload):
            self.terminal_event_seen = True
            if self._terminal_elapsed_ms is None:
                self._terminal_elapsed_ms = self._elapsed_ms(observed_at - self._started_at)

    def _elapsed_ms(self, value: float) -> int:
        """Convert a monotonic interval to a bounded non-negative integer."""

        if value <= 0:
            return 0
        return min(7 * 24 * 60 * 60 * 1000, int(value * 1000))

    def _record_event_type(self, event_type: str) -> None:
        if event_type in self.event_type_counts:
            self.event_type_counts[event_type] += 1
            return
        if len(self.event_type_counts) >= SSE_EVENT_TYPE_TELEMETRY_LIMIT:
            self.event_types_truncated = True
            return
        self.event_type_counts[event_type] = 1


RESPONSES_TERMINAL_EVENT_TYPES = {
    "response.completed",
    "response.failed",
    "response.incomplete",
    "error",
}


def _responses_terminal_observer(
    event_name: str | None,
    data: bytes,
    payload: Any,
) -> bool:
    """Responses protocol terminal predicate: [DONE] or known terminal types."""
    if data == b"[DONE]":
        return True
    event_type = event_name
    if isinstance(payload, Mapping):
        payload_type = payload.get("type")
        if isinstance(payload_type, str) and payload_type:
            event_type = payload_type
    return event_type in RESPONSES_TERMINAL_EVENT_TYPES


def _chat_terminal_observer(
    event_name: str | None,
    data: bytes,
    payload: Any,
) -> bool:
    """Chat Completions protocol terminal predicate: only the [DONE] sentinel."""
    del event_name, payload
    return data == b"[DONE]"


def _responses_events_have_terminal(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type in RESPONSES_TERMINAL_EVENT_TYPES:
            return True
    return False


def _responses_event_starts_downstream_output(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type in {"response.output_text.delta", "response.reasoning_summary_text.delta"}:
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.output_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.reasoning_summary_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.function_call_arguments.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.function_call_arguments.done":
        return True
    if event_type == "response.custom_tool_call_input.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.custom_tool_call_input.done":
        return True
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item")
        return isinstance(item, Mapping) and item.get("type") in {"function_call", "custom_tool_call", "message"}
    return False


def _responses_event_commits_downstream_output(event: Mapping[str, Any], upstream_name: str) -> bool:
    event_type = event.get("type")
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.output_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.refusal.done":
        refusal = event.get("refusal")
        return isinstance(refusal, str) and bool(refusal)
    if event_type == "response.reasoning_summary_text.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.reasoning_summary_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.output_item.done":
        item = event.get("item")
        return isinstance(item, Mapping) and item.get("type") == "reasoning"
    return False


def _responses_output_item_has_visible_or_tool_output(item: Mapping[str, Any]) -> bool:
    item_type = item.get("type")
    if item_type in {"function_call", "custom_tool_call"}:
        return _responses_completed_tool_item(item) is not None
    # `tool_search_call` is a client-executed Responses output item.  The
    # Gateway must not execute or rewrite it, but it is still a completed
    # control/tool item and therefore makes a third-party response non-empty.
    # Without this classification the relay buffers the entire stream and
    # rejects the valid `response.completed` as an empty response, causing
    # Codex to reconnect before it can submit `tool_search_output`.
    if item_type == "tool_search_call":
        return (
            isinstance(item.get("call_id"), str)
            and bool(item.get("call_id"))
            and item.get("execution") == "client"
        )
    if item_type == "message":
        return bool(_message_item_visible_text(item))
    return False


def _responses_completed_event_has_visible_or_tool_output(event: Mapping[str, Any]) -> bool:
    if event.get("type") != "response.completed":
        return False
    response = event.get("response")
    if not isinstance(response, Mapping):
        return False
    output = response.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if isinstance(item, Mapping) and _responses_output_item_has_visible_or_tool_output(item):
            return True
    return False


def _responses_event_has_visible_or_tool_output(event: Mapping[str, Any], upstream_name: str) -> bool:
    event_type = event.get("type")
    if upstream_name != "official":
        if _is_reasoning_text_stream_event(event):
            return False
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "reasoning":
                return False
    if _responses_event_commits_downstream_output(event, upstream_name):
        return True
    if _is_reasoning_text_stream_event(event):
        delta = event.get("delta")
        return upstream_name == "official" and isinstance(delta, str) and bool(delta)
    if event_type in {
        "response.function_call_arguments.delta",
        "response.custom_tool_call_input.delta",
    }:
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type in {
        "response.function_call_arguments.done",
        "response.custom_tool_call_input.done",
    }:
        return True
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item")
        return isinstance(item, Mapping) and _responses_output_item_has_visible_or_tool_output(item)
    if event_type == "response.completed":
        return _responses_completed_event_has_visible_or_tool_output(event)
    return False


def _responses_event_is_tool_call_construction(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type in {
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
    }:
        return True
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item")
        return isinstance(item, Mapping) and item.get("type") in {"function_call", "custom_tool_call"}
    return False


def _responses_completed_tool_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    call_id = item.get("call_id")
    name = item.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        return None
    if item_type == "function_call":
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            return None
        return dict(item)
    if item_type == "custom_tool_call":
        tool_input = item.get("input")
        if not isinstance(tool_input, str):
            return None
        return dict(item)
    return None


def _synthetic_response_completed_from_tool_items(
    *,
    created_response: Mapping[str, Any] | None,
    model: str,
    output_items: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    completed_items = [
        completed
        for item in output_items
        if isinstance(item, Mapping)
        for completed in [_responses_completed_tool_item(item)]
        if completed is not None
    ]
    if not completed_items:
        return None
    response = dict(created_response or {})
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id:
        response_id = f"resp_{uuid.uuid4().hex[:12]}"
    response["id"] = response_id
    response.setdefault("object", "response")
    response["status"] = "completed"
    response["model"] = response.get("model") if isinstance(response.get("model"), str) else model
    response["output"] = completed_items
    return {"type": "response.completed", "response": response}


def _responses_sse_line_resets_idle_timeout(line: bytes) -> bool:
    event = _parse_sse_json_payload(line)
    if not isinstance(event, Mapping):
        return False
    event_type = event.get("type")
    return isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error")


def _stream_error_event_detail(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        code = error.get("code")
        if isinstance(message, str) and message:
            return f"{code}: {message}" if code is not None else message
        return json.dumps(error, ensure_ascii=True, separators=(",", ":"))[:300]
    if isinstance(error, str) and error:
        return error[:300]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))[:300]


def _responses_stream_error_detail(event: Mapping[str, Any]) -> str:
    response = event.get("response")
    if isinstance(response, Mapping):
        error = response.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            code = error.get("code")
            if isinstance(message, str) and message:
                return f"{code}: {message}" if code is not None else message
            return json.dumps(error, ensure_ascii=True, separators=(",", ":"))[:300]
        if isinstance(error, str) and error:
            return error[:300]
    return _stream_error_event_detail(event)


def _responses_stream_error_type(event: Mapping[str, Any]) -> str | None:
    event_type = event.get("type")
    return event_type if event_type in {"error", "response.failed", "response.incomplete"} else None


def _chat_stream_error_detail(payload: Mapping[str, Any]) -> str | None:
    if "error" not in payload:
        return None
    return _stream_error_event_detail(payload)


def _chat_stream_chunk_has_finish(chunk: Mapping[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if isinstance(choice, Mapping) and choice.get("finish_reason") is not None:
            return True
    return False


def _chat_stream_chunk_starts_downstream_output(chunk: Mapping[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            return True
        if isinstance(delta.get("tool_calls"), list) and delta.get("tool_calls"):
            return True
        message = choice.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str) and content:
                return True
            if isinstance(message.get("tool_calls"), list) and message.get("tool_calls"):
                return True
    return False


def _chat_stream_chunks_have_terminal(chunks: list[Mapping[str, Any] | str]) -> bool:
    for chunk in chunks:
        if chunk == "[DONE]":
            return True
        if isinstance(chunk, Mapping) and _chat_stream_chunk_has_finish(chunk):
            return True
    return False


def _sse_json_line(payload: Mapping[str, Any], line_ending: bytes) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + line_ending


def _chat_stream_status_chunk(
    status: Mapping[str, Any],
    model: str | None,
) -> dict[str, Any]:
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": IMAGE_PROXY_PROGRESS_TEXT},
                "finish_reason": None,
            }
        ],
        "codexhub_status": dict(status),
    }


def _responses_stream_status_event(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "output_index": 0,
        "content_index": 0,
        "delta": IMAGE_PROXY_PROGRESS_TEXT,
        "codexhub_status": dict(status),
    }


def _downstream_stream_status_payload(
    inbound_format: str,
    status: Mapping[str, Any],
    model: str | None,
) -> dict[str, Any]:
    if inbound_format == "chat_completions":
        return _chat_stream_status_chunk(status, model)
    return _responses_stream_status_event(status)


def _chat_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    fragments = _collect_text_fragments(value)
    return "\n".join(fragments)


def _tail_text_for_compact_detection(payload: Mapping[str, Any], inbound_format: str) -> str:
    if inbound_format == "chat_completions":
        messages = payload.get("messages")
        if isinstance(messages, list):
            fragments: list[str] = []
            for message in messages[-5:]:
                if isinstance(message, Mapping):
                    fragments.append(_chat_content_text(message.get("content")))
            return "\n".join(fragment for fragment in fragments if fragment)

    input_items = payload.get("input")
    if isinstance(input_items, list):
        return "\n".join(_collect_text_fragments(input_items[-5:]))
    return "\n".join(_collect_text_fragments(input_items))


def _is_compact_summary_payload(payload: Mapping[str, Any], inbound_format: str) -> bool:
    text = _tail_text_for_compact_detection(payload, inbound_format).lower()
    if not text:
        return False

    summary_prompt = (
        "detailed summary of the conversation so far" in text
        or "create a detailed summary of the conversation" in text
        or "compact summary" in text
    )
    text_only_instruction = "do not call any tools" in text or "respond with text only" in text
    summary_shape = "<summary>" in text or "summary should include" in text
    return summary_prompt and text_only_instruction and summary_shape


def _request_kind_from_headers_and_payload(
    headers: Mapping[str, str] | Any,
    payload: Mapping[str, Any] | None,
    inbound_format: str,
) -> str:
    for header_name in ("x-request-kind", "x-query-source"):
        header_value = _get_header(headers, header_name)
        if isinstance(header_value, str) and header_value.strip().lower() == RETRY_REQUEST_COMPACT:
            return RETRY_REQUEST_COMPACT
    if isinstance(payload, Mapping) and _is_compact_summary_payload(payload, inbound_format):
        return RETRY_REQUEST_COMPACT
    return RETRY_REQUEST_MAIN_GENERATION


def _strip_tools_for_text_only_proxy_payload(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
    event_name: str = "text_only_proxy_tools_stripped",
) -> bool:
    removed_tools = payload.pop("tools", None)
    removed_tool_choice = payload.pop("tool_choice", None)
    if removed_tools is None and removed_tool_choice is None:
        return False

    removed_tool_count = len(removed_tools) if isinstance(removed_tools, list) else 0
    _write_adapter_event(
        event_context,
        event_name,
        upstream=upstream_name,
        model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        removed_tool_count=removed_tool_count,
        removed_tool_choice=removed_tool_choice if isinstance(removed_tool_choice, str) else None,
    )
    return True


def _strip_tools_for_compact_payload(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
) -> bool:
    return _strip_tools_for_text_only_proxy_payload(
        payload,
        event_context=event_context,
        upstream_name=upstream_name,
        event_name="compact_text_only_tools_stripped",
    )


def _chat_completion_body_is_empty(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or "error" in payload:
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return False
        if not isinstance(content, str) and _chat_content_text(content).strip():
            return False
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return False
    return True


def _responses_body_is_empty(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or "error" in payload:
        return False
    output = payload.get("output")
    if not isinstance(output, list) or not output:
        return True
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "function_call":
            return False
        if item.get("type") != "message":
            continue
        if _chat_content_text(item.get("content")).strip():
            return False
    return True


def _compact_response_body_is_empty(body: bytes, inbound_format: str) -> bool:
    if inbound_format == "chat_completions":
        return _chat_completion_body_is_empty(body)
    return _responses_body_is_empty(body)


def _downstream_json_error_body(
    *,
    message: str,
    error_type: str,
    code: str,
    upstream_name: str,
) -> bytes:
    return json.dumps(
        {
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
                "upstream": upstream_name,
            }
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _incomplete_stream_json_error_body(upstream_name: str) -> bytes:
    return _downstream_json_error_body(
        message="Upstream stream ended before a terminal event.",
        error_type="upstream_stream_incomplete",
        code="upstream_stream_incomplete",
        upstream_name=upstream_name,
    )


class UpstreamProtocolTranslationError(ValueError):
    """Marks an unsupported upstream wire shape for the downstream error mapper."""

    def __init__(self, cause: UnsupportedProtocolTranslationError):
        self.cause = cause
        super().__init__(str(cause))


_responses_content_to_chat_content = responses_content_to_chat_content
_responses_input_to_chat_messages = responses_input_to_chat_messages
_responses_tools_to_chat_tools = responses_tools_to_chat_tools
_responses_tool_choice_to_chat_tool_choice = responses_tool_choice_to_chat_tool_choice


def _responses_request_to_chat_completion_body(
    body: bytes,
    *,
    drop_client_metadata: bool = False,
    drop_client_transport_fields: bool = False,
    drop_reasoning: bool = False,
) -> bytes:
    return responses_request_to_chat_completion_body(
        body,
        drop_client_metadata=drop_client_metadata,
        drop_client_transport_fields=drop_client_transport_fields,
        drop_reasoning=drop_reasoning,
    )


XMLISH_TOOL_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*['\"]([^'\"]+)['\"]\s*>(.*?)</invoke>",
    re.IGNORECASE | re.DOTALL,
)
XMLISH_TOOL_ARG_RE = re.compile(
    r"<([A-Za-z_][A-Za-z0-9_.-]*)\s*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
MODEL_STREAM_TAG_RE = re.compile(r"\]<\][A-Za-z0-9_.:-]+\[>")


def _strip_model_stream_tags(text: str) -> str:
    return MODEL_STREAM_TAG_RE.sub("", text)


def _xmlish_tool_call_outputs_from_text(text: str) -> list[dict[str, Any]]:
    cleaned = _strip_model_stream_tags(text)
    if "<invoke" not in cleaned.lower():
        return []
    output: list[dict[str, Any]] = []
    for match in XMLISH_TOOL_INVOKE_RE.finditer(cleaned):
        name = html.unescape(match.group(1)).strip()
        if not _valid_tool_name(name):
            continue
        arguments: dict[str, Any] = {}
        for arg_match in XMLISH_TOOL_ARG_RE.finditer(match.group(2)):
            key = arg_match.group(1).strip()
            if key.lower() in {"tool_call", "invoke"}:
                continue
            value = html.unescape(_strip_model_stream_tags(arg_match.group(2))).strip()
            arguments[key] = value
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        output.append(
            {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=True, separators=(",", ":")),
            }
        )
    return output


def _repair_chat_completion_response_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _hide_reasoning_text(payload)
    payload, _ = _normalize_third_party_tool_call(payload)
    payload, _ = _downgrade_invalid_third_party_tool_calls(payload)
    return payload


def _chat_completion_to_response_body(body: bytes, *, repair: bool = True) -> bytes:
    try:
        return chat_completion_to_response_body(
            body,
            repair=repair,
            chat_content_text=_chat_content_text,
            xmlish_tool_outputs=_xmlish_tool_call_outputs_from_text,
            repair_response=_repair_chat_completion_response_payload,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _normalize_chat_function_call_name(name: str) -> str:
    if name == f"{NODE_REPL_NAMESPACE}.js":
        return f"{NODE_REPL_NAMESPACE}__js"
    if name == f"{NODE_REPL_NAMESPACE}__js":
        return name
    tool_name = THIRD_PARTY_TOOL_NAME_ALIASES.get(name)
    if tool_name in MULTI_AGENT_TOOL_NAMES:
        return f"multi_agent_v1__{tool_name}"
    return name


def _chat_stream_chunks_to_response_events(chunks: list[Mapping[str, Any] | str]) -> list[dict[str, Any]]:
    try:
        return chat_stream_chunks_to_response_events(
            chunks,
            normalize_function_name=_normalize_chat_function_call_name,
            xmlish_tool_outputs=_xmlish_tool_call_outputs_from_text,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _response_events_shape_summary(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    tool_items: list[dict[str, Any]] = []
    output_items: list[dict[str, Any]] = []
    terminal_count = 0
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str):
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
            if event_type == "response.completed":
                terminal_count += 1
        item = event.get("item")
        if isinstance(item, Mapping):
            item_summary = {
                "event_type": event_type,
                "type": item.get("type"),
                "name": item.get("name"),
                "namespace": item.get("namespace"),
                "call_id": item.get("call_id"),
                "has_arguments": bool(item.get("arguments")),
            }
            output_items.append(item_summary)
            if item.get("type") == "function_call":
                tool_items.append(item_summary)
        response = event.get("response")
        if isinstance(response, Mapping):
            output = response.get("output")
            if isinstance(output, list):
                for output_item in output:
                    if not isinstance(output_item, Mapping):
                        continue
                    item_summary = {
                        "event_type": event_type,
                        "type": output_item.get("type"),
                        "name": output_item.get("name"),
                        "namespace": output_item.get("namespace"),
                        "call_id": output_item.get("call_id"),
                        "has_arguments": bool(output_item.get("arguments")),
                    }
                    output_items.append(item_summary)
                    if output_item.get("type") == "function_call":
                        tool_items.append(item_summary)
    return {
        "event_count": len(events),
        "event_type_counts": type_counts,
        "terminal_count": terminal_count,
        "output_items": output_items[:12],
        "output_item_count": len(output_items),
        "tool_items": tool_items[:12],
        "tool_item_count": len(tool_items),
    }


def _chat_stream_shape_summary(chunks: list[Mapping[str, Any] | str]) -> dict[str, Any]:
    text_parts: list[str] = []
    reasoning_chars = 0
    source_key_counts: dict[str, int] = {}
    finish_reason_counts: dict[str, int] = {}
    tool_call_names: list[str] = []
    summary: dict[str, Any] = {
        "chunk_count": len(chunks),
        "done_count": 0,
        "choice_count": 0,
        "delta_source_count": 0,
        "message_source_count": 0,
        "content_source_count": 0,
        "tool_call_count": 0,
        "tool_call_id_count": 0,
        "tool_call_name_count": 0,
        "tool_call_argument_chars": 0,
        "reasoning_source_count": 0,
        "reasoning_chars": 0,
        "text_chars": 0,
        "xmlish_tool_count": 0,
    }

    for chunk in chunks:
        if chunk == "[DONE]":
            summary["done_count"] += 1
            continue
        if not isinstance(chunk, Mapping):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        summary["choice_count"] += len(choices)
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                key = str(finish_reason)[:80]
                finish_reason_counts[key] = finish_reason_counts.get(key, 0) + 1
            for source_name in ("delta", "message"):
                source = choice.get(source_name)
                if not isinstance(source, Mapping):
                    continue
                summary[f"{source_name}_source_count"] += 1
                for key in source.keys():
                    key_text = str(key)[:80]
                    source_key_counts[key_text] = source_key_counts.get(key_text, 0) + 1
                content = source.get("content")
                text = content if isinstance(content, str) else _chat_content_text(content)
                if text:
                    summary["content_source_count"] += 1
                    text_parts.append(text)
                for key, value in source.items():
                    if "reason" not in str(key).lower():
                        continue
                    summary["reasoning_source_count"] += 1
                    if isinstance(value, str):
                        reasoning_chars += len(value)
                    elif value is not None:
                        reasoning_chars += len(str(value))
                tool_calls = source.get("tool_calls")
                if not isinstance(tool_calls, list):
                    continue
                summary["tool_call_count"] += len(tool_calls)
                for tool_call in tool_calls:
                    if not isinstance(tool_call, Mapping):
                        continue
                    if isinstance(tool_call.get("id"), str) and tool_call.get("id"):
                        summary["tool_call_id_count"] += 1
                    function = tool_call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    if isinstance(function.get("name"), str) and function.get("name"):
                        summary["tool_call_name_count"] += 1
                        if len(tool_call_names) < 12:
                            tool_call_names.append(function["name"])
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        summary["tool_call_argument_chars"] += len(arguments)

    text = "".join(text_parts)
    summary["text_chars"] = len(text)
    summary["reasoning_chars"] = reasoning_chars
    summary["xmlish_tool_count"] = len(_xmlish_tool_call_outputs_from_text(text)) if text else 0
    summary["finish_reasons"] = finish_reason_counts
    summary["source_keys"] = source_key_counts
    summary["tool_call_names"] = tool_call_names
    if text:
        summary["text_hmac"] = proxy_telemetry.telemetry_hmac(
            RUNTIME_CODEX_DIR,
            b"chat-stream-text",
            text.encode("utf-8", errors="ignore"),
        )
    return summary


CHAT_RAW_REASONING_FIELDS = frozenset({"reasoning", "reasoning_content"})


def _suppress_chat_reasoning_extensions(
    chunks: list[Mapping[str, Any] | str],
    *,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> tuple[list[Mapping[str, Any] | str], bool]:
    """Drop third-party Chat reasoning extensions before Responses conversion."""
    rewritten_chunks: list[Mapping[str, Any] | str] = []
    field_count = 0
    chunk_count = 0

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            rewritten_chunks.append(chunk)
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            rewritten_chunks.append(chunk)
            continue

        rewritten_choices: list[Any] = []
        chunk_changed = False
        for choice in choices:
            if not isinstance(choice, Mapping):
                rewritten_choices.append(choice)
                continue
            rewritten_choice: Mapping[str, Any] = choice
            for source_name in ("delta", "message"):
                source = choice.get(source_name)
                if not isinstance(source, Mapping):
                    continue
                fields = CHAT_RAW_REASONING_FIELDS.intersection(source)
                if not fields:
                    continue
                if rewritten_choice is choice:
                    rewritten_choice = dict(choice)
                rewritten_source = dict(source)
                for field in fields:
                    rewritten_source.pop(field, None)
                rewritten_choice[source_name] = rewritten_source
                field_count += len(fields)
                chunk_changed = True
            rewritten_choices.append(rewritten_choice)

        if chunk_changed:
            rewritten_chunk = dict(chunk)
            rewritten_chunk["choices"] = rewritten_choices
            rewritten_chunks.append(rewritten_chunk)
            chunk_count += 1
        else:
            rewritten_chunks.append(chunk)

    if not field_count:
        return chunks, False
    _write_adapter_event(
        event_context,
        "chat_reasoning_extensions_suppressed",
        upstream=upstream_name,
        field_count=field_count,
        chunk_count=chunk_count,
    )
    return rewritten_chunks, True


def _chat_stream_is_empty_lifecycle_final(
    summary: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> bool:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return False
    return int(summary.get("text_chars") or 0) == 0 and int(summary.get("tool_call_count") or 0) == 0


FINAL_REPORT_LINE_PREFIXES = (
    ("RESULT:", "SENTINEL:", "SUBAGENT_CHAIN:"),
    ("SPAWNED:", "AGENT_ID:", "SENTINEL_SEEN:", "CLOSED:"),
    (
        "SPAWN_COUNT:",
        "AGENT_IDS:",
        "SENTINEL_A_SEEN:",
        "SENTINEL_B_SEEN:",
        "CLOSED_COUNT:",
        "EXTRA_SPAWN:",
    ),
)


def _final_report_nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.strip().splitlines() if line.strip()]


def _lines_match_final_report_prefixes(lines: list[str], start: int, prefixes: tuple[str, ...]) -> bool:
    if start + len(prefixes) > len(lines):
        return False
    for offset, prefix in enumerate(prefixes):
        if not lines[start + offset].upper().startswith(prefix):
            return False
    return True


def _lifecycle_final_format_violation(text: str) -> bool:
    lines = _final_report_nonempty_lines(text)
    if not lines:
        return False
    for prefixes in FINAL_REPORT_LINE_PREFIXES:
        for start in range(len(lines)):
            if not _lines_match_final_report_prefixes(lines, start, prefixes):
                continue
            return start != 0 or len(lines) != len(prefixes)
    return False


def _text_contains_lifecycle_final_report(text: str) -> bool:
    lines = _final_report_nonempty_lines(text)
    if not lines:
        return False
    for prefixes in FINAL_REPORT_LINE_PREFIXES:
        for start in range(len(lines)):
            if _lines_match_final_report_prefixes(lines, start, prefixes):
                return True
    return False


def _chat_stream_visible_text(chunks: list[Mapping[str, Any] | str]) -> str:
    text_parts: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            for source_name in ("delta", "message"):
                source = choice.get(source_name)
                if not isinstance(source, Mapping):
                    continue
                content = source.get("content")
                text = content if isinstance(content, str) else _chat_content_text(content)
                if text:
                    text_parts.append(text)
    return "".join(text_parts).strip()


def _response_payload_visible_text(payload: Any) -> str:
    text_parts: list[str] = []
    if not isinstance(payload, Mapping):
        return ""
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, Mapping) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts.append(_chat_content_text(content))
    return "\n".join(part.strip() for part in text_parts if part.strip()).strip()


def _response_payload_tool_call_count(payload: Any) -> int:
    if not isinstance(payload, Mapping):
        return 0
    count = 0
    output = payload.get("output")
    if isinstance(output, list):
        count += sum(1 for item in output if isinstance(item, Mapping) and item.get("type") == "function_call")
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                count += len(tool_calls)
    return count


def _response_body_lifecycle_final_issue(
    body: bytes,
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> str | None:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return None
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if _response_payload_tool_call_count(payload) > 0:
        return None
    text = _response_payload_visible_text(payload)
    if not text:
        return "empty"
    if _lifecycle_final_format_violation(text):
        return "format"
    return None


def _responses_events_lifecycle_final_issue(
    events: list[Mapping[str, Any]],
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> str | None:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return None
    return _response_body_lifecycle_final_issue(_events_to_responses_body(events), event_context, request_kind)


def _chat_stream_lifecycle_final_issue(
    chunks: list[Mapping[str, Any] | str],
    summary: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> str | None:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return None
    if int(summary.get("tool_call_count") or 0) > 0:
        return None
    if int(summary.get("text_chars") or 0) == 0:
        return "empty"
    if _lifecycle_final_format_violation(_chat_stream_visible_text(chunks)):
        return "format"
    return None


def _raise_lifecycle_final_issue(upstream_name: str, issue: str) -> None:
    if issue == "empty":
        raise LifecycleEmptyFinalResponseError(upstream_name)
    if issue == "format":
        raise LifecycleFinalFormatResponseError(upstream_name)


def _lifecycle_final_issue_event_name(issue: str) -> str:
    if issue == "empty":
        return "lifecycle_empty_final_resample"
    return "lifecycle_final_format_resample"


def _lifecycle_final_issue_missing_reason(issue: str) -> str:
    if issue == "empty":
        return "lifecycle_empty_final_response"
    return "lifecycle_final_format_response"


_chat_content_to_responses_content = chat_content_to_responses_content
_chat_messages_to_responses_input = partial(
    chat_messages_to_responses_input,
    chat_content_text=_chat_content_text,
)


def _normalize_responses_string_input(payload: dict[str, Any]) -> bool:
    value = payload.get("input")
    if not isinstance(value, str):
        return False
    payload["input"] = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": value}],
        }
    ]
    return True


def _normalize_responses_message_input_items(payload: dict[str, Any]) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    normalized_items: list[Any] = []
    for item in input_items:
        if (
            isinstance(item, dict)
            and item.get("type") is None
            and isinstance(item.get("role"), str)
            and "content" in item
        ):
            rewritten = dict(item)
            rewritten["type"] = "message"
            normalized_items.append(rewritten)
            changed = True
        else:
            normalized_items.append(item)

    if changed:
        payload["input"] = normalized_items
    return changed


_chat_tools_to_responses_tools = chat_tools_to_responses_tools
_chat_tool_choice_to_responses_tool_choice = chat_tool_choice_to_responses_tool_choice


def _chat_completions_request_to_responses_body(body: bytes) -> bytes:
    return chat_completions_request_to_responses_body(
        body,
        chat_content_text=_chat_content_text,
    )


def _chat_function_name_from_response_item(item: Mapping[str, Any]) -> str | None:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None
    namespace = item.get("namespace")
    if namespace == "multi_agent_v1":
        return f"multi_agent_v1__{name}"
    if namespace == NODE_REPL_NAMESPACE:
        return f"{NODE_REPL_NAMESPACE}__{name}"
    flat_codex_apps_alias = _codex_apps_namespace_flat_alias(namespace, name)
    if flat_codex_apps_alias is not None:
        return flat_codex_apps_alias
    if isinstance(namespace, str) and _supports_explicit_namespace_alias(namespace) and _valid_tool_name(name):
        alias = f"{namespace}__{name}"
        if _valid_tool_name(alias):
            return alias
    return name


def _response_body_to_chat_completion_body(body: bytes) -> bytes:
    try:
        return response_body_to_chat_completion_body(
            body,
            function_name_from_response_item=_chat_function_name_from_response_item,
            error_body=_chat_completion_error_body,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _chat_completion_body_to_stream_chunks(body: bytes) -> list[dict[str, Any]]:
    try:
        return chat_completion_body_to_stream_chunks(body)
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _chat_completion_error_body(payload: Mapping[str, Any]) -> bytes:
    return chat_completion_error_body(payload)


class UpstreamStreamInterruptedError(RuntimeError):
    """Raised when an upstream stream is interrupted before downstream output starts."""

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(str(cause))

class UpstreamEmptyCompletedResponseError(UpstreamStreamIncompleteError):
    """Raised when a third-party Responses stream completes with no visible output."""


class UpstreamStreamErrorEvent(RuntimeError):
    """Raised when an upstream Responses SSE stream emits an error event."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        super().__init__(_stream_error_event_detail(payload))


class DownstreamClosedError(RuntimeError):
    """Raised when a downstream closure aborts Gateway output for a request."""


class DownstreamClosedBeforeRetryError(DownstreamClosedError):
    """Raised when a downstream closure aborts an upstream retry attempt."""


class DownstreamClosedDuringImageProxyError(DownstreamClosedError):
    """Raised when a downstream closure aborts image-proxy preprocessing."""


class DownstreamKeepaliveFailedError(DownstreamClosedError):
    """Raised when a downstream keepalive write fails, aborting upstream iteration."""


RESPONSES_TERMINAL_EVENT_TYPES = {
    "response.completed",
    "response.failed",
    "response.incomplete",
    "error",
}


def _responses_events_have_terminal(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type in RESPONSES_TERMINAL_EVENT_TYPES:
            return True
    return False


def _chat_stream_chunk_has_finish(chunk: Mapping[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if isinstance(choice, Mapping) and choice.get("finish_reason") is not None:
            return True
    return False


def _chat_stream_chunks_have_terminal(chunks: list[Mapping[str, Any] | str]) -> bool:
    for chunk in chunks:
        if chunk == "[DONE]":
            return True
        if isinstance(chunk, Mapping) and _chat_stream_chunk_has_finish(chunk):
            return True
    return False


def _response_events_to_chat_stream_chunks(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
) -> list[dict[str, Any]]:
    try:
        return response_events_to_chat_stream_chunks(
            events,
            require_completed=require_completed,
            function_name_from_response_item=_chat_function_name_from_response_item,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


class _ResponsesToChatStreamConverter(ResponsesToChatStreamConverter):
    def chunks_for_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return super().chunks_for_event(event)
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc


class _ChatToResponsesStreamConverter(ChatToResponsesStreamConverter):
    def events_for_chunk(self, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return super().events_for_chunk(chunk)
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc

    def events_for_done(self) -> list[dict[str, Any]]:
        try:
            return super().events_for_done()
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc


def _is_reasoning_sse_payload(payload: Mapping[str, Any] | None) -> bool:
    if payload is None:
        return False
    event_type = payload.get("type")
    if isinstance(event_type, str) and "reasoning" in event_type:
        return True
    item = payload.get("item")
    return isinstance(item, dict) and item.get("type") == "reasoning"


def _events_to_responses_body(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
) -> bytes:
    return events_to_responses_body(
        events,
        require_completed=require_completed,
        usage_from_response=_usage_from_payload,
    )


def _response_body_to_response_sse_events(body: bytes) -> list[dict[str, Any]]:
    return response_body_to_response_sse_events(
        body,
        collect_text_fragments=_collect_text_fragments,
    )


def _count_sse_reasoning_event(
    stats: dict[str, Any],
    original_payload: Mapping[str, Any] | None,
    rewritten_payload: Mapping[str, Any] | None,
) -> None:
    if not _is_reasoning_sse_payload(original_payload) and not _is_reasoning_sse_payload(rewritten_payload):
        return

    stats["seen"] = True
    original_type = original_payload.get("type") if original_payload is not None else None
    rewritten_type = rewritten_payload.get("type") if rewritten_payload is not None else None
    if isinstance(original_type, str):
        counts = stats["original_event_counts"]
        counts[original_type] = counts.get(original_type, 0) + 1
    if isinstance(rewritten_type, str):
        counts = stats["rewritten_event_counts"]
        counts[rewritten_type] = counts.get(rewritten_type, 0) + 1

    delta_payload = rewritten_payload if rewritten_payload is not None else original_payload
    delta = delta_payload.get("delta") if delta_payload is not None else None
    if isinstance(delta, str):
        stats["delta_events"] += 1
        stats["delta_chars"] += len(delta)


def _compatible_compaction_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    seen: set[str] = set()
    fragments: list[str] = []
    for fragment in _collect_text_fragments(dict(item)):
        if fragment not in seen:
            seen.add(fragment)
            fragments.append(fragment)

    if not fragments:
        return _developer_text_message(
            "[Compacted conversation context — opaque, details unavailable]"
        )

    return _developer_text_message("[Compacted conversation context]\n" + "\n\n".join(fragments))


def _developer_text_message(content: str) -> dict[str, str]:
    return {"type": "message", "role": "developer", "content": content}


LIFECYCLE_FINAL_RETRY_GUIDANCE = """Codex native subagent final report correction
status: lifecycle_complete_final_retry
previous_attempt_status: the previous lifecycle-complete assistant response did not satisfy the requested visible final format.
visible_response_required: re-emit only the final report requested by the user, as ordinary assistant message content.
final_format_required: the first visible output token must be the first token of that requested final report. Do not include headings, bullets, summaries, markdown fences, or prose before or after the report.
tool_calls_forbidden: the subagent lifecycle already completed via real current-turn tool executions; do not call tool_search, node_repl, local tools, or any multi_agent_v1 tool again.
source_of_truth: use only the observed current-turn agent ids, sentinels, wait results, and close state already present in the transcript.
"""


def _lifecycle_final_retry_guidance_message(reason: str) -> dict[str, str]:
    return _developer_text_message(LIFECYCLE_FINAL_RETRY_GUIDANCE + f"retry_reason: {reason}")


def _responses_body_with_lifecycle_final_retry_guidance(body: bytes, reason: str) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    input_items = payload.get("input")
    guidance = _lifecycle_final_retry_guidance_message(reason)
    if isinstance(input_items, list):
        payload["input"] = list(input_items) + [guidance]
    elif isinstance(input_items, str):
        payload["input"] = [_user_text_message(input_items), guidance]
    else:
        payload["input"] = [guidance]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


WORKER_SUBAGENT_FINALIZATION_GUIDANCE = """Codex native worker subagent finalization guidance
status: worker_subagent_finalization_required
visible_response_required: after completing any required tool work, emit the worker result as ordinary assistant message content, not only reasoning, hidden notes, or tool arguments. If you emit an empty message, the coordinator receives no result and will treat the worker as incomplete.
allowed_status_prefixes: DONE, DONE_WITH_CONCERNS, NEEDS_CONTEXT, BLOCKED, PASS, FAIL
required_next_action_after_tools: use the exact report format requested by the worker task. For diagnostic implementer/reviewer tasks, the first visible output token should usually be DONE, PASS, FAIL, or BLOCKED.
do_not_spawn_subagents: this is a worker subagent request, not a coordinator request.
"""


def _worker_subagent_finalization_message() -> dict[str, str]:
    return _developer_text_message(WORKER_SUBAGENT_FINALIZATION_GUIDANCE)


def _has_worker_subagent_finalization_guidance(value: Any) -> bool:
    return any(
        "worker_subagent_finalization_required" in fragment
        for fragment in _collect_text_fragments(value)
    )


def _stringify_internal_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value).strip()


def _append_internal_field(lines: list[str], label: str, value: Any) -> None:
    text = _stringify_internal_field(value)
    if not text:
        return
    lines.append(f"{label}:")
    lines.append(text)


def _single_line_internal_field(value: Any) -> str:
    text = _stringify_internal_field(value)
    return " ".join(text.split()) if text else ""


def _valid_tool_name(value: Any) -> bool:
    return isinstance(value, str) and bool(TOOL_NAME_RE.fullmatch(value))


def _is_tool_call_item(item: Mapping[str, Any]) -> bool:
    item_type = item.get("type")
    return isinstance(item_type, str) and item_type in {"function_call", "custom_tool_call"}


def _has_invalid_tool_name(item: Mapping[str, Any]) -> bool:
    return _is_tool_call_item(item) and not _valid_tool_name(item.get("name"))


def _transcript_text(title: str, item: Mapping[str, Any]) -> str:
    lines = [title]
    for label, key in (
        ("type", "type"),
        ("namespace", "namespace"),
        ("name", "name"),
        ("call_id", "call_id"),
        ("status", "status"),
    ):
        value = _stringify_internal_field(item.get(key))
        if value:
            lines.append(f"{label}: {value}")
    _append_internal_field(lines, "input", item.get("input"))
    _append_internal_field(lines, "arguments", item.get("arguments"))
    _append_internal_field(lines, "output", item.get("output"))
    _append_internal_field(lines, "action", item.get("action"))
    _append_internal_field(lines, "execution", item.get("execution"))
    _append_internal_field(lines, "tools", item.get("tools"))
    return "\n".join(lines)


def _assistant_transcript_message(title: str, item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": _transcript_text(title, item)}],
    }


def _json_object_from_arguments(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed, _end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _json_argument_string_needs_repair(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError:
        parsed_obj = _json_object_from_arguments(value)
        return parsed_obj is not None
    return not isinstance(parsed, dict)


def _dump_arguments_like(original: Any, arguments: Mapping[str, Any]) -> Any:
    if isinstance(original, str):
        return json.dumps(arguments, ensure_ascii=True, separators=(",", ":"))
    return dict(arguments)


def _tool_schema_name(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    name = value.get("name")
    return name if isinstance(name, str) and name else None


def _tool_parameters_schema(value: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("parameters", "inputSchema", "input_schema"):
        schema = value.get(key)
        if isinstance(schema, dict):
            return dict(schema)
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _explicit_function_tool(name: str, description: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": dict(parameters),
    }


def _multi_agent_explicit_function_tools(
    include_spawn_agent: bool = True,
    include_wait_agent: bool = True,
    include_close_agent: bool = True,
    include_resume_agent: bool = True,
    include_send_input: bool = True,
    open_agent_ids: list[str] | None = None,
    wait_agent_ids: list[str] | None = None,
    close_agent_ids: list[str] | None = None,
    worker_selector_values: tuple[str, ...] = ("worker", "general"),
) -> list[dict[str, Any]]:
    namespace = MULTI_AGENT_DISCOVERY_TOOLS[0]
    tools = namespace.get("tools") if isinstance(namespace, Mapping) else None
    if not isinstance(tools, list):
        return []

    result: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = _tool_schema_name(tool)
        if not name or name not in MULTI_AGENT_TOOL_NAMES:
            continue
        if name == "spawn_agent" and not include_spawn_agent:
            continue
        if name == "wait_agent" and not include_wait_agent:
            continue
        if name == "close_agent" and not include_close_agent:
            continue
        if name == "resume_agent" and not include_resume_agent:
            continue
        if name == "send_input" and not include_send_input:
            continue
        alias = f"multi_agent_v1__{name}"
        description = str(tool.get("description") or f"Invoke Codex multi_agent_v1.{name}.")
        parameters = json.loads(json.dumps(_tool_parameters_schema(tool)))
        properties = parameters.setdefault("properties", {})
        if name == "spawn_agent" and isinstance(properties, dict):
            agent_type = properties.get("agent_type")
            if isinstance(agent_type, dict):
                agent_type["enum"] = list(worker_selector_values)
            message = properties.get("message")
            if isinstance(message, dict):
                message.setdefault(
                    "description",
                    "Complete child-agent task prompt. Include all instructions the child needs.",
                )
            fork_context = properties.get("fork_context")
            if isinstance(fork_context, dict):
                fork_context["description"] = (
                    "Set false for self-contained child prompts so the child follows only the supplied message. "
                    "Set true only when inheriting the coordinator transcript is explicitly needed."
                )
                fork_context.setdefault("default", False)
        target_agent_ids = open_agent_ids
        if name == "wait_agent" and wait_agent_ids is not None:
            target_agent_ids = wait_agent_ids
        elif name == "close_agent" and close_agent_ids is not None:
            target_agent_ids = close_agent_ids
        if target_agent_ids and name in {"wait_agent", "close_agent"}:
            ids_text = ", ".join(target_agent_ids)
            description += f" Current open agent_id target(s): {ids_text}. Use these id(s) next."
            if isinstance(properties, dict):
                if name == "wait_agent":
                    targets = properties.get("targets")
                    if isinstance(targets, dict):
                        targets["description"] = (
                            f"MUST be exactly this list for the currently open Codex child agent(s): {list(target_agent_ids)!r}."
                        )
                        targets.setdefault("default", list(target_agent_ids))
                        items = targets.setdefault("items", {})
                        if isinstance(items, dict):
                            items["enum"] = list(target_agent_ids)
                    timeout_ms = properties.get("timeout_ms")
                    if isinstance(timeout_ms, dict):
                        timeout_ms.setdefault("description", "Use 60000 for the standard Codex subagent test.")
                        timeout_ms.setdefault("default", 60000)
                elif name == "close_agent":
                    target = properties.get("target")
                    if isinstance(target, dict):
                        target["description"] = (
                            f"MUST be one of the already-waited open Codex child agent id(s): {', '.join(target_agent_ids)}."
                        )
                        if len(target_agent_ids) == 1:
                            target.setdefault("default", target_agent_ids[0])
                        target["enum"] = list(target_agent_ids)
        result.append(_explicit_function_tool(alias, description, parameters))
    return result


def _supports_explicit_namespace_alias(namespace_name: str) -> bool:
    return namespace_name == "codex_app" or namespace_name.startswith("mcp__")


def _is_multi_agent_namespace_name(name: str | None) -> bool:
    return isinstance(name, str) and name in MULTI_AGENT_NAMESPACE_ALIASES


def _is_multi_agent_explicit_tool_name(name: str) -> bool:
    return name in THIRD_PARTY_TOOL_NAME_ALIASES


def _multi_agent_alias_tool_name(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    if name in MULTI_AGENT_TOOL_NAMES:
        return name
    return THIRD_PARTY_TOOL_NAME_ALIASES.get(name)


def _looks_like_response_tool_name_fragment(value: Mapping[str, Any]) -> bool:
    item_type = value.get("type")
    if isinstance(item_type, str) and item_type.startswith("response."):
        return True
    if any(key in value for key in ("call_id", "item_id", "arguments", "status")):
        return True
    return set(value.keys()).issubset({"name", "namespace", "index", "id"})


def _is_multi_agent_tool_schema(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    item_type = value.get("type")
    name = _tool_schema_name(value)
    if item_type == "namespace":
        return _is_multi_agent_namespace_name(name)
    if item_type == "function":
        if value.get("namespace") == "multi_agent_v1":
            return True
        return isinstance(name, str) and _is_multi_agent_explicit_tool_name(name)
    return False


def _is_node_repl_explicit_tool_name(name: str) -> bool:
    return name.startswith(f"{NODE_REPL_NAMESPACE}__") or name.startswith(f"{NODE_REPL_NAMESPACE}.")


def _is_node_repl_tool_schema(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    item_type = value.get("type")
    name = _tool_schema_name(value)
    if item_type == "namespace":
        return name == NODE_REPL_NAMESPACE
    if item_type == "function":
        if value.get("namespace") == NODE_REPL_NAMESPACE:
            return True
        return isinstance(name, str) and _is_node_repl_explicit_tool_name(name)
    return False


def _is_local_tool_gateway_tool_schema(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    name = _tool_schema_name(value)
    if not isinstance(name, str):
        return False
    local_gateway_namespace = "mcp__codex_apps__local_tool_gateway_"
    if value.get("type") == "namespace":
        return name == local_gateway_namespace
    if value.get("type") == "function":
        namespace = value.get("namespace")
        if namespace == local_gateway_namespace:
            return True
        return name.startswith(f"{local_gateway_namespace}__")
    return False


def _is_mcp_or_codex_app_tool_schema(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    name = _tool_schema_name(value)
    namespace = value.get("namespace")
    if isinstance(namespace, str) and (namespace.startswith("mcp__") or namespace == "codex_app"):
        return True
    if not isinstance(name, str):
        return False
    return name.startswith("mcp__") or name == "codex_app" or name.startswith("codex_app__")


def _is_flattened_namespace_schema(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("type") != "namespace":
        return False
    name = _tool_schema_name(value)
    return _is_multi_agent_namespace_name(name) or (
        isinstance(name, str) and _supports_explicit_namespace_alias(name)
    )


def _is_raw_namespace_schema(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("type") == "namespace"


def _flatten_namespace_function_tools(tools: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for namespace in tools:
        if not isinstance(namespace, Mapping) or namespace.get("type") != "namespace":
            continue
        namespace_name = _tool_schema_name(namespace)
        namespace_tools = namespace.get("tools")
        if (
            not namespace_name
            or not _valid_tool_name(namespace_name)
            or not _supports_explicit_namespace_alias(namespace_name)
            or not isinstance(namespace_tools, list)
        ):
            continue
        for tool in namespace_tools:
            if not isinstance(tool, Mapping) or tool.get("type") != "function":
                continue
            tool_name = _tool_schema_name(tool)
            if not tool_name or not _valid_tool_name(tool_name):
                continue
            alias = f"{namespace_name}__{tool_name}"
            description = str(tool.get("description") or f"Invoke Codex namespace {namespace_name}.{tool_name}.")
            result.append(_explicit_function_tool(alias, description, _tool_parameters_schema(tool)))
    return result


def _multi_agent_function_call_name(item: Mapping[str, Any]) -> str | None:
    if item.get("type") != "function_call":
        return None

    namespace = item.get("namespace")
    name = item.get("name")
    tool_name = _multi_agent_alias_tool_name(name)
    if namespace == "multi_agent_v1" and tool_name is not None:
        return tool_name
    if tool_name is not None and name != tool_name:
        return tool_name
    return None


def _node_repl_function_call_name(item: Mapping[str, Any]) -> str | None:
    if item.get("type") != "function_call":
        return None

    namespace = item.get("namespace")
    name = item.get("name")
    if namespace == NODE_REPL_NAMESPACE and name == "js":
        return "js"
    if name in {f"{NODE_REPL_NAMESPACE}__js", f"{NODE_REPL_NAMESPACE}.js"}:
        return "js"
    return None


def _external_tool_protocol(upstream: Mapping[str, Any]) -> str:
    configured = str(upstream.get("tool_protocol") or "auto").strip().lower()
    if configured in TOOL_PROTOCOLS and configured != "auto":
        return configured
    upstream_format = str(upstream.get("upstream_format") or "").strip().lower()
    if upstream_format == "responses":
        return "responses_structured"
    if upstream_format == "chat_completions":
        return "chat_tools"
    return "text_compat"


def _external_tool_surface_strategy(upstream: Mapping[str, Any]) -> str:
    configured = upstream.get("tool_surface_strategy", "eager")
    if isinstance(configured, str) and configured in TOOL_SURFACE_STRATEGIES:
        return configured
    write_proxy_event("external_tool_surface_rejected", reason="invalid_tool_surface_strategy")
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            TOOL_SURFACE_STRATEGY_ERROR_CODE,
            "External tool surface strategy is invalid.",
        )
    )


def _external_native_responses_tool_codec(upstream: Mapping[str, Any]) -> str:
    configured = upstream.get("native_responses_tool_codec", "none")
    if isinstance(configured, str) and configured in NATIVE_RESPONSES_TOOL_CODECS:
        return configured
    write_proxy_event("native_responses_tool_codec_rejected", reason="invalid_codec")
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE,
            "External native Responses tool codec is invalid.",
        )
    )


_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY = "_runtime_tool_compatibility_plan"
_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY = "_runtime_tool_compatibility_stream"
_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY = "_runtime_tool_compatibility_attempt_generation"
_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY = "_runtime_tool_compatibility_attempt_plan"
_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY = (
    "_runtime_tool_compatibility_attempt_plan_generation"
)
_RUNTIME_TOOL_CAPABILITY_MANIFEST_ERROR_CODE = "tool_compatibility_capability_manifest"


def _raise_malformed_runtime_tool_capability_manifest() -> NoReturn:
    raise RuntimeToolCompatibilityError(
        _RUNTIME_TOOL_CAPABILITY_MANIFEST_ERROR_CODE,
        "malformed_capability_manifest",
    )


def _validate_runtime_tool_capability_facts(facts: Mapping[str, Any]) -> None:
    boolean_keys = {
        "function_lifecycle",
        "supports_functions",
        "namespace_lifecycle",
        "supports_namespace",
        "supports_namespaces",
        "custom_lifecycle",
        "supports_custom",
        "supports_custom_tools",
        "tool_search_lifecycle",
        "supports_tool_search",
        "accepts_namespace_adapter",
        "namespace_adapter",
        "accepts_custom_adapter",
        "custom_adapter",
        "accepts_tool_search_adapter",
        "tool_search_adapter",
    }
    for key in boolean_keys:
        if key in facts and type(facts[key]) is not bool:
            _raise_malformed_runtime_tool_capability_manifest()

    for key in ("hosted_lifecycles", "hosted_kinds", "unknown_lifecycles", "unknown_kinds"):
        if key not in facts:
            continue
        value = facts[key]
        if isinstance(value, str):
            continue
        if isinstance(value, Mapping):
            if any(not isinstance(name, str) or type(enabled) is not bool for name, enabled in value.items()):
                _raise_malformed_runtime_tool_capability_manifest()
            continue
        if isinstance(value, (bytes, bytearray)) or not isinstance(value, IterableABC):
            _raise_malformed_runtime_tool_capability_manifest()
        if any(not isinstance(name, str) for name in value):
            _raise_malformed_runtime_tool_capability_manifest()

    for key in ("max_tool_name_length", "max_alias_attempts"):
        if key in facts and (type(facts[key]) is not int or facts[key] <= 0):
            _raise_malformed_runtime_tool_capability_manifest()


def _runtime_tool_protocol_capabilities(
    tool_protocol: str,
    upstream: Mapping[str, Any],
) -> RuntimeProtocolCapabilities:
    try:
        supplied = upstream.get("tool_protocol_capabilities")
        if supplied is not None and not isinstance(supplied, Mapping):
            _raise_malformed_runtime_tool_capability_manifest()
        if isinstance(supplied, Mapping):
            _validate_runtime_tool_capability_facts(supplied)
        facts = supplied if isinstance(supplied, Mapping) else None
        # A capability manifest is authoritative only for predicates it
        # explicitly states.  Responses and chat-completions retain the
        # conservative plain-function + adapter baseline.  Text-compatible
        # endpoints have no native lifecycle by default; every capability
        # must be explicit.
        if tool_protocol == "text_compat":
            baseline_protocol = "none"
        elif tool_protocol == "responses_structured":
            baseline_protocol = "chat_tools"
        else:
            baseline_protocol = tool_protocol
        if facts is not None:
            return RuntimeProtocolCapabilities.for_protocol(baseline_protocol, facts)
        if baseline_protocol in {"chat_tools", "chat", "chat_completions"}:
            return RuntimeProtocolCapabilities.chat_tools()
        return RuntimeProtocolCapabilities()
    except RuntimeToolCompatibilityError:
        raise
    except (AttributeError, OverflowError, TypeError, ValueError):
        _raise_malformed_runtime_tool_capability_manifest()


def _raise_runtime_tool_compatibility_error(error: RuntimeToolCompatibilityError) -> NoReturn:
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(error.code, str(error))
    ) from error


def _prepare_runtime_tool_compatibility(
    payload: dict[str, Any],
    upstream: Mapping[str, Any],
    tool_protocol: str,
    event_context: dict[str, Any],
    native_responses_tool_codec: str | None = None,
) -> bool:
    tools = payload.get("tools")
    declarations = tools if isinstance(tools, list) else []
    codec = (
        native_responses_tool_codec
        if native_responses_tool_codec is not None
        else _external_native_responses_tool_codec(upstream)
    )
    if tool_protocol == "responses_structured" and codec == "strict_apply_patch":
        apply_patch_tools = [
            tool
            for tool in declarations
            if isinstance(tool, Mapping) and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
        ]
        if len(apply_patch_tools) > 1:
            _raise_native_responses_tool_contract_error(
                event_context,
                codec=codec,
                reason="duplicate_declaration",
                count=len(apply_patch_tools),
            )
        if apply_patch_tools:
            apply_patch_tool = apply_patch_tools[0]
            if apply_patch_tool.get("type") != "custom":
                _raise_native_responses_tool_contract_error(
                    event_context,
                    codec=codec,
                    reason="declaration_not_custom",
                )
            _validate_strict_apply_patch_custom_tool(
                apply_patch_tool,
                event_context,
                codec=codec,
            )
    planned_declarations = [
        declaration
        for declaration in declarations
        if not (
            isinstance(declaration, Mapping)
            and declaration.get("name") == APPLY_PATCH_FUNCTION_NAME
            and (
                codec == "strict_apply_patch"
                or not isinstance(declaration.get("format"), Mapping)
            )
        )
    ]
    try:
        plan = build_tool_compatibility_plan(
            planned_declarations,
            selected_protocol=tool_protocol,
            provider_hosted_capabilities=upstream.get("hosted_tool_capabilities"),
            tool_choice=payload.get("tool_choice"),
            protocol_capabilities=_runtime_tool_protocol_capabilities(tool_protocol, upstream),
            request_token=uuid.uuid4().hex,
        )
    except RuntimeToolCompatibilityError as exc:
        write_proxy_event(
            "runtime_tool_compatibility_rejected",
            surface=exc.surface,
            outcome="rejected",
            count=1,
        )
        _raise_runtime_tool_compatibility_error(exc)
    event_context[_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY] = plan
    event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY, None)
    event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY, None)
    event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY, None)
    write_proxy_event(
        "runtime_tool_compatibility_planned",
        counts=plan.diagnostics.as_dict()["counts"],
    )
    return False


def _apply_runtime_tool_compatibility_plan(
    payload: dict[str, Any],
    plan: RuntimeToolCompatibilityPlan,
) -> bool:
    try:
        encoded = plan.encode_payload(payload)
    except RuntimeToolCompatibilityError as exc:
        _raise_runtime_tool_compatibility_error(exc)
    if encoded == payload:
        return False
    payload.clear()
    payload.update(encoded)
    return True


def _runtime_tool_compatibility_plan(
    event_context: Mapping[str, Any] | None,
) -> RuntimeToolCompatibilityPlan | None:
    value = (event_context or {}).get(_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY)
    return value if isinstance(value, RuntimeToolCompatibilityPlan) else None


def _runtime_tool_compatibility_plan_for_attempt(
    event_context: Mapping[str, Any] | None,
) -> RuntimeToolCompatibilityPlan | None:
    """Resolve the immutable request plan into the current relay attempt.

    The request plan owns stable aliases and declaration classification.  Each
    permitted upstream retry receives a shallow plan copy with a fresh call
    ownership ledger; no route or provider selection is performed here.
    """
    request_plan = _runtime_tool_compatibility_plan(event_context)
    if request_plan is None or not isinstance(event_context, dict):
        return request_plan
    generation = event_context.get(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY)
    if generation is None:
        return request_plan
    attempt_plan = event_context.get(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY)
    planned_generation = event_context.get(
        _RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY
    )
    if (
        not isinstance(attempt_plan, RuntimeToolCompatibilityPlan)
        or planned_generation != generation
        or attempt_plan is request_plan
    ):
        attempt_plan = request_plan.new_attempt()
        event_context[_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY] = attempt_plan
        event_context[_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY] = generation
        event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY, None)
    return attempt_plan


def _runtime_tool_compatibility_stream_for_attempt(
    event_context: Mapping[str, Any] | None,
) -> tuple[RuntimeToolCompatibilityPlan | None, Any | None]:
    """Return the attempt-local stream ledger shared by both relay surfaces."""
    plan = _runtime_tool_compatibility_plan_for_attempt(event_context)
    if plan is None or not isinstance(event_context, dict):
        return plan, None
    stream = event_context.get(_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY)
    if stream is None or getattr(stream, "plan", None) is not plan:
        stream = plan.new_stream()
        event_context[_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY] = stream
    return plan, stream


def _runtime_required_tool_diagnostics(
    plan: RuntimeToolCompatibilityPlan | None,
    tool_choice_name: Any,
) -> tuple[str, str]:
    """Return bounded family/disposition fields for required-tool telemetry."""
    if plan is None or not isinstance(tool_choice_name, str):
        return "unknown", "unknown"

    record = plan.registry.record_for_alias(tool_choice_name)
    if record is not None:
        family = record.family
        disposition = next(
            (
                entry.disposition
                for entry in plan.entries
                if entry.declaration_index == record.declaration_index
            ),
            "unknown",
        )
    else:
        matches = [
            entry for entry in plan.entries if entry.original_name == tool_choice_name
        ]
        if len(matches) != 1:
            return "unknown", "unknown"
        family = matches[0].family
        disposition = matches[0].disposition

    bounded_families = {
        "plain_function",
        "namespace",
        "custom_freeform",
        "tool_search",
        "selected_provider_hosted",
        "unknown_future_kind",
    }
    bounded_dispositions = {
        "native",
        "adapt",
        "omit",
        "required-but-unavailable",
    }
    return (
        family if family in bounded_families else "unknown",
        disposition if disposition in bounded_dispositions else "unknown",
    )


def _runtime_alias_matches_namespace(
    plan: RuntimeToolCompatibilityPlan | None,
    tool: Any,
    namespace: str,
) -> bool:
    if plan is None or not isinstance(tool, Mapping):
        return False
    record = plan.registry.record_for_alias(_tool_schema_name(tool))
    return record is not None and record.namespace == namespace


def _runtime_alias_for_namespace_child(
    plan: RuntimeToolCompatibilityPlan | None,
    namespace: str,
    child_name: str,
) -> str | None:
    if plan is None:
        return None
    for alias in plan.aliases:
        record = plan.registry.record_for_alias(alias)
        if record is not None and record.namespace == namespace and record.child_name == child_name:
            return alias
    return None


def _runtime_plan_has_native_plain_function(
    plan: RuntimeToolCompatibilityPlan | None,
    item: Mapping[str, Any],
) -> bool:
    name = item.get("name")
    return bool(
        plan is not None
        and isinstance(name, str)
        and any(
            entry.family == "plain_function"
            and entry.disposition == "native"
            and entry.original_name == name
            for entry in plan.entries
        )
    )


def _rewrite_generated_guidance_tool_name(value: Any, original: str, alias: str) -> Any:
    if isinstance(value, str):
        return value.replace(original, alias)
    if isinstance(value, list):
        return [_rewrite_generated_guidance_tool_name(item, original, alias) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _rewrite_generated_guidance_tool_name(item, original, alias)
            for key, item in value.items()
        }
    return value


STRICT_APPLY_PATCH_EXAMPLE = """*** Begin Patch
*** Update File: example.txt
@@
-before
+after
*** End Patch"""
STRICT_APPLY_PATCH_CUSTOM_TOOL_FIELDS = frozenset(
    {"type", "name", "description", "format"}
)
STRICT_APPLY_PATCH_FORMAT_FIELDS = frozenset(
    {"type", "syntax", "definition"}
)


def _raise_native_responses_tool_contract_error(
    event_context: Mapping[str, Any] | None,
    *,
    codec: str,
    reason: str,
    count: int = 1,
) -> NoReturn:
    _write_adapter_event(
        event_context,
        "native_responses_tool_codec",
        codec=codec,
        outcome="rejected",
        count=count,
        reason=reason,
    )
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            NATIVE_RESPONSES_TOOL_CONTRACT_ERROR_CODE,
            "External native Responses apply_patch declaration is ambiguous or lossy.",
        )
    )


def _validate_strict_apply_patch_custom_tool(
    tool: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    codec: str,
) -> None:
    if set(tool) != STRICT_APPLY_PATCH_CUSTOM_TOOL_FIELDS:
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="custom_tool_fields_not_exact",
        )
    description = tool.get("description")
    tool_format = tool.get("format")
    if not isinstance(description, str) or not description.strip():
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="missing_description",
        )
    if not isinstance(tool_format, Mapping) or set(tool_format) != STRICT_APPLY_PATCH_FORMAT_FIELDS:
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="format_fields_not_exact",
        )
    if tool_format.get("type") != "grammar":
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="format_not_grammar",
        )
    if not isinstance(tool_format.get("syntax"), str) or not tool_format["syntax"].strip():
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="missing_grammar_syntax",
        )
    if not isinstance(tool_format.get("definition"), str) or not tool_format["definition"].strip():
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="missing_grammar_definition",
        )


def _strict_apply_patch_function_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    description_parts: list[str] = []
    description = tool.get("description")
    if isinstance(description, str) and description.strip():
        description_parts.append(description.strip())
    tool_format = tool.get("format")
    if isinstance(tool_format, Mapping):
        grammar = tool_format.get("definition")
        if isinstance(grammar, str) and grammar.strip():
            syntax = tool_format.get("syntax")
            grammar_label = f"Original freeform grammar ({syntax}):" if isinstance(syntax, str) and syntax else "Original freeform grammar:"
            description_parts.append(f"{grammar_label}\n{grammar.strip()}")
    description_parts.append(
        "Provide the complete patch in the required `patch` string. "
        f"Example:\n{STRICT_APPLY_PATCH_EXAMPLE}"
    )
    return {
        "type": "function",
        "name": APPLY_PATCH_FUNCTION_NAME,
        "description": "\n\n".join(description_parts),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"patch": {"type": "string", "minLength": 1}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    }


def _adapt_native_responses_tool_declarations(
    payload: dict[str, Any],
    upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    codec: str | None = None,
) -> bool:
    codec = codec or _external_native_responses_tool_codec(upstream)
    if codec == "none":
        return False
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False

    apply_patch_tools = [
        tool
        for tool in tools
        if isinstance(tool, Mapping) and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
    ]
    if len(apply_patch_tools) > 1:
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="duplicate_declaration",
            count=len(apply_patch_tools),
        )
    if not apply_patch_tools:
        _write_adapter_event(
            event_context,
            "native_responses_tool_codec",
            codec=codec,
            outcome="untouched",
            count=0,
        )
        return False
    apply_patch_tool = apply_patch_tools[0]
    if apply_patch_tool.get("type") != "custom":
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="declaration_not_custom",
        )
    _validate_strict_apply_patch_custom_tool(
        apply_patch_tool,
        event_context,
        codec=codec,
    )

    rewritten_tools: list[Any] = []
    adapted = 0
    for tool in tools:
        if (
            isinstance(tool, Mapping)
            and tool.get("type") == "custom"
            and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
        ):
            rewritten_tools.append(_strict_apply_patch_function_tool(tool))
            adapted += 1
        else:
            rewritten_tools.append(tool)
    if not adapted:
        return False
    payload["tools"] = rewritten_tools
    _write_adapter_event(
        event_context,
        "native_responses_tool_codec",
        codec=codec,
        outcome="adapted",
        count=adapted,
    )
    return True


def _structured_tool_function_call_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if item.get("type") != "function_call":
        return None
    request_shape = dict(item)
    for response_only_field in ("id", "status", WORKER_REQUESTED_BINDING_FIELD):
        request_shape.pop(response_only_field, None)
    tool_name = _multi_agent_function_call_name(item)
    if tool_name is not None:
        rewritten = request_shape
        rewritten.pop("namespace", None)
        rewritten["name"] = f"multi_agent_v1__{tool_name}"
        normalized, _, args_changed = _normalize_multi_agent_arguments(rewritten.get("arguments"), tool_name)
        if args_changed:
            rewritten["arguments"] = normalized
        return rewritten
    node_name = _node_repl_function_call_name(item)
    if node_name is not None:
        rewritten = request_shape
        rewritten.pop("namespace", None)
        rewritten["name"] = f"{NODE_REPL_NAMESPACE}__{node_name}"
        return rewritten
    return request_shape


def _same_selected_v1_collaboration_function_call(
    item: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> bool:
    """Allow current V1 calls through the legacy structured-call adapter."""

    if (
        item.get("type") != "function_call"
        or not isinstance(event_context, Mapping)
        or event_context.get("collaboration_protocol") != _COLLABORATION_V1
    ):
        return False
    try:
        return _classify_collaboration_payload({"input": [item]}) == _COLLABORATION_V1
    except _CollaborationBoundaryError:
        return False


def _hoist_additional_tools_input_items(payload: dict[str, Any]) -> bool:
    """Promote Codex's internal tool carrier to the standard Responses field."""
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    promoted_tools: list[Any] = []
    rewritten_items: list[Any] = []
    changed = False
    for item in input_items:
        if not isinstance(item, Mapping) or item.get("type") != "additional_tools":
            rewritten_items.append(item)
            continue
        item_tools = item.get("tools")
        if isinstance(item_tools, list):
            promoted_tools.extend(item_tools)
        changed = True

    if not changed:
        return False

    tools = payload.get("tools")
    if isinstance(tools, list):
        tools.extend(promoted_tools)
    elif tools is None:
        payload["tools"] = promoted_tools
    else:
        # The caller's top-level tools are already malformed; remove the
        # internal-only item so it cannot be forwarded to a third party.
        payload["tools"] = promoted_tools
    payload["input"] = rewritten_items
    return True


def _rewrite_structured_tool_input_items(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
    compatibility_plan: RuntimeToolCompatibilityPlan | None = None,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    input_items, adapted_apply_patch_call_ids, changed = _adapt_apply_patch_custom_tool_history(
        input_items,
        event_context=event_context,
    )
    if changed:
        payload["input"] = input_items
    rewritten_items: list[Any] = []
    preserved_structured_call_ids: set[str] = set(adapted_apply_patch_call_ids)
    available_function_names = _function_tool_names(payload.get("tools"))
    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue
        if (
            compatibility_plan is not None
            and compatibility_plan.owns_wire_value(item)
            and not _same_selected_v1_collaboration_function_call(item, event_context)
            and not _runtime_plan_has_native_plain_function(compatibility_plan, item)
        ):
            call_id = item.get("call_id")
            if isinstance(call_id, str):
                preserved_structured_call_ids.add(call_id)
            rewritten_items.append(item)
            continue
        if item.get("type") == "function_call":
            function_name = item.get("name")
            call_id = item.get("call_id")
            preserve_apply_patch_history = (
                function_name == APPLY_PATCH_FUNCTION_NAME
                and isinstance(call_id, str)
                and call_id in adapted_apply_patch_call_ids
            )
            preserve_available_function = (
                isinstance(function_name, str) and function_name in available_function_names
            )
            if (
                preserve_available_function
                or preserve_apply_patch_history
                or _multi_agent_function_call_name(item) is not None
                or _node_repl_function_call_name(item) is not None
            ):
                if isinstance(call_id, str):
                    preserved_structured_call_ids.add(call_id)
                rewritten = _structured_tool_function_call_item(item)
                rewritten_items.append(rewritten if rewritten is not None else item)
                changed = changed or rewritten != item
            else:
                replacement = _compatible_internal_message(item)
                if replacement is not None:
                    rewritten_items.append(replacement)
                changed = True
            continue
        if item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id in preserved_structured_call_ids:
                rewritten_items.append(dict(item))
            else:
                replacement = _compatible_internal_message(item)
                if replacement is not None:
                    rewritten_items.append(replacement)
                changed = True
            continue
        item_type = item.get("type")
        replacement = _compatible_internal_message(item)
        if replacement is not None:
            rewritten_items.append(replacement)
            changed = True
        elif isinstance(item_type, str) and item_type in INTERNAL_INPUT_ITEM_TYPES:
            # Internal item (e.g. reasoning, compaction_trigger) with no text
            # replacement — drop it instead of leaking the raw item upstream.
            changed = True
        else:
            rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
        _write_adapter_event(
            event_context,
            "structured_tool_input_items_rewritten",
            upstream=upstream_name,
        )
    return changed


def _inject_explicit_codex_tools(
    payload: dict[str, Any],
    include_tool_search: bool = True,
    include_multi_agent_tools: bool = True,
    include_spawn_agent: bool = True,
    include_wait_agent: bool = True,
    include_close_agent: bool = True,
    include_resume_agent: bool = True,
    include_send_input: bool = True,
    include_node_repl_tools: bool = True,
    include_local_tool_gateway_tools: bool = True,
    strip_namespace_tools: bool = True,
    strip_all_namespace_tools: bool = False,
    include_flattened_namespace_tools: bool = True,
    tool_surface_counts: dict[str, int] | None = None,
    open_agent_ids: list[str] | None = None,
    wait_agent_ids: list[str] | None = None,
    close_agent_ids: list[str] | None = None,
    worker_selector_values: tuple[str, ...] = ("worker", "general"),
) -> bool:
    if tool_surface_counts is not None:
        tool_surface_counts.update(
            {
                "namespace_declaration_count": 0,
                "eager_tool_count": 0,
                "retained_core_count": 0,
                "deferred_tool_count": 0,
            }
        )
    tools = payload.get("tools")
    if tools is None:
        tools = []
        payload["tools"] = tools
    if not isinstance(tools, list):
        return False

    changed = False
    caller_non_namespace_tools = tuple(
        tool
        for tool in tools
        if not (isinstance(tool, Mapping) and tool.get("type") == "namespace")
    )
    namespace_declaration_count = sum(1 for tool in tools if _is_flattened_namespace_schema(tool))
    flattened_namespace_tools = _flatten_namespace_function_tools(tools)
    if strip_namespace_tools:
        # Eager preserves the #105 compatibility surface: only declarations the
        # existing flattener understands are removed. deferred_core is the
        # explicit normalized surface and drops every raw namespace declaration.
        namespace_to_strip = (
            _is_raw_namespace_schema if strip_all_namespace_tools else _is_flattened_namespace_schema
        )
        filtered_tools = [tool for tool in tools if not namespace_to_strip(tool)]
        if len(filtered_tools) != len(tools):
            tools[:] = filtered_tools
            changed = True

    if not include_local_tool_gateway_tools:
        filtered_tools = [tool for tool in tools if not _is_local_tool_gateway_tool_schema(tool)]
        if len(filtered_tools) != len(tools):
            tools[:] = filtered_tools
            changed = True
        flattened_namespace_tools = [
            tool for tool in flattened_namespace_tools if not _is_local_tool_gateway_tool_schema(tool)
        ]

    if not include_multi_agent_tools:
        filtered_tools = [tool for tool in tools if not _is_multi_agent_tool_schema(tool)]
        if len(filtered_tools) != len(tools):
            tools[:] = filtered_tools
            changed = True

    if not include_node_repl_tools:
        filtered_tools = [tool for tool in tools if not _is_node_repl_tool_schema(tool)]
        if len(filtered_tools) != len(tools):
            tools[:] = filtered_tools
            changed = True

    excluded_tool_names = set()
    if not include_tool_search:
        excluded_tool_names.add(TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["name"])
    if not include_multi_agent_tools:
        excluded_tool_names.update(f"multi_agent_v1__{tool_name}" for tool_name in MULTI_AGENT_TOOL_NAMES)
    if not include_spawn_agent:
        excluded_tool_names.add("multi_agent_v1__spawn_agent")
    if not include_wait_agent:
        excluded_tool_names.add("multi_agent_v1__wait_agent")
    if not include_close_agent:
        excluded_tool_names.add("multi_agent_v1__close_agent")
    if not include_resume_agent:
        excluded_tool_names.add("multi_agent_v1__resume_agent")
    if not include_send_input:
        excluded_tool_names.add("multi_agent_v1__send_input")
    if excluded_tool_names:
        filtered_tools = [
            tool
            for tool in tools
            if not (
                isinstance(tool, Mapping)
                and tool.get("type") == "function"
                and tool.get("name") in excluded_tool_names
            )
        ]
        if len(filtered_tools) != len(tools):
            tools[:] = filtered_tools
            changed = True

    existing_names = {_tool_schema_name(tool) for tool in tools}
    existing_names.discard(None)
    core_additions = []
    if include_tool_search:
        core_additions.append(TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL)
    if include_multi_agent_tools:
        core_additions.extend(
            _multi_agent_explicit_function_tools(
                include_spawn_agent=include_spawn_agent,
                include_wait_agent=include_wait_agent,
                include_close_agent=include_close_agent,
                include_resume_agent=include_resume_agent,
                include_send_input=include_send_input,
                open_agent_ids=open_agent_ids,
                wait_agent_ids=wait_agent_ids,
                close_agent_ids=close_agent_ids,
                worker_selector_values=worker_selector_values,
            )
        )
    if not include_multi_agent_tools:
        core_additions = [tool for tool in core_additions if not _is_multi_agent_tool_schema(tool)]
        flattened_namespace_tools = [
            tool for tool in flattened_namespace_tools if not _is_multi_agent_tool_schema(tool)
        ]
    if not include_node_repl_tools:
        core_additions = [tool for tool in core_additions if not _is_node_repl_tool_schema(tool)]
        flattened_namespace_tools = [
            tool for tool in flattened_namespace_tools if not _is_node_repl_tool_schema(tool)
        ]
    if excluded_tool_names:
        core_additions = [
            tool
            for tool in core_additions
            if not (
                isinstance(tool, Mapping)
                and tool.get("type") == "function"
                and tool.get("name") in excluded_tool_names
            )
        ]
        flattened_namespace_tools = [
            tool
            for tool in flattened_namespace_tools
            if not (
                isinstance(tool, Mapping)
                and tool.get("type") == "function"
                and tool.get("name") in excluded_tool_names
            )
        ]

    potential_names = set(existing_names)
    for tool in core_additions:
        name = _tool_schema_name(tool)
        if name:
            potential_names.add(name)
    deferred_tool_count = 0
    for tool in flattened_namespace_tools:
        name = _tool_schema_name(tool)
        if name and name not in potential_names:
            potential_names.add(name)
            deferred_tool_count += 1

    flattened_tool_ids = {id(tool) for tool in flattened_namespace_tools}
    additions = list(core_additions)
    if include_flattened_namespace_tools:
        additions.extend(flattened_namespace_tools)

    eager_tool_count = 0
    for tool in additions:
        name = _tool_schema_name(tool)
        if not name:
            continue
        replaced_existing = False
        if name in existing_names:
            for index, existing_tool in enumerate(tools):
                if not isinstance(existing_tool, Mapping) or _tool_schema_name(existing_tool) != name:
                    continue
                if name.startswith("multi_agent_v1__") and dict(existing_tool) != tool:
                    tools[index] = tool
                    changed = True
                replaced_existing = True
                break
        if replaced_existing:
            continue
        tools.append(tool)
        existing_names.add(name)
        if id(tool) in flattened_tool_ids:
            eager_tool_count += 1
        changed = True
    if tool_surface_counts is not None:
        surviving_tool_ids = {id(tool) for tool in tools}
        tool_surface_counts.update(
            {
                "namespace_declaration_count": namespace_declaration_count,
                "eager_tool_count": eager_tool_count if include_flattened_namespace_tools else 0,
                "retained_core_count": sum(
                    1 for tool in caller_non_namespace_tools if id(tool) in surviving_tool_ids
                ),
                "deferred_tool_count": deferred_tool_count if not include_flattened_namespace_tools else 0,
            }
        )
    return changed


def _filter_tools_for_subagent_coordinator(
    payload: dict[str, Any],
    *,
    include_node_repl_tools: bool,
    compatibility_plan: RuntimeToolCompatibilityPlan | None = None,
) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [
        tool
        for tool in tools
        if _is_multi_agent_tool_schema(tool)
        or _runtime_alias_matches_namespace(compatibility_plan, tool, "multi_agent_v1")
        or (
            include_node_repl_tools
            and (
                _is_node_repl_tool_schema(tool)
                or _runtime_alias_matches_namespace(compatibility_plan, tool, NODE_REPL_NAMESPACE)
            )
        )
    ]
    if len(filtered_tools) == len(tools):
        return False
    payload["tools"] = filtered_tools
    return True


def _filter_tools_for_subagent_worker(
    payload: dict[str, Any],
    *,
    compatibility_plan: RuntimeToolCompatibilityPlan | None = None,
) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [
        tool
        for tool in tools
        if not _is_multi_agent_tool_schema(tool)
        and not _is_mcp_or_codex_app_tool_schema(tool)
        and not any(
            _runtime_alias_matches_namespace(compatibility_plan, tool, namespace)
            for namespace in (
                NODE_REPL_NAMESPACE,
                "multi_agent_v1",
                "mcp__multi_agent_v1",
            )
        )
        and _tool_schema_name(tool) != TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["name"]
    ]
    if len(filtered_tools) == len(tools):
        return False
    payload["tools"] = filtered_tools
    return True


def _hide_tools_for_completed_subagent_lifecycle(payload: dict[str, Any]) -> bool:
    changed = False
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        payload["tools"] = []
        changed = True
    elif "tools" not in payload:
        payload["tools"] = []
        changed = True
    if payload.pop("tool_choice", None) is not None:
        changed = True
    return changed


def _restrict_tools_to_required_tool(payload: dict[str, Any], tool_name: str | None) -> bool:
    if not tool_name:
        return False
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [tool for tool in tools if _tool_schema_name(tool) == tool_name]
    if not filtered_tools or len(filtered_tools) == len(tools):
        return False
    payload["tools"] = filtered_tools
    return True


def _required_subagent_tool_choice(
    *,
    tool_protocol: str,
    lifecycle_complete: bool,
    include_spawn_agent: bool,
    include_wait_agent: bool,
    include_close_agent: bool,
    include_resume_agent: bool,
    include_send_input: bool,
    include_node_repl_for_subagent_workflow: bool,
) -> str | None:
    if tool_protocol not in {"chat_tools", "responses_structured"} or lifecycle_complete:
        return None
    if include_node_repl_for_subagent_workflow:
        return None
    candidates: list[str] = []
    if include_spawn_agent:
        candidates.append("multi_agent_v1__spawn_agent")
    if include_wait_agent:
        candidates.append("multi_agent_v1__wait_agent")
    if include_close_agent:
        candidates.append("multi_agent_v1__close_agent")
    if include_send_input:
        candidates.append("multi_agent_v1__send_input")
    elif include_resume_agent:
        candidates.append("multi_agent_v1__resume_agent")
    return candidates[0] if len(candidates) == 1 else None


def _set_required_subagent_tool_choice(
    payload: dict[str, Any],
    tool_name: str | None,
    *,
    event_context: Mapping[str, Any] | None,
    upstream: Any,
) -> bool:
    if not tool_name:
        return False
    desired = {"type": "function", "name": tool_name}
    if payload.get("tool_choice") == desired:
        return False
    payload["tool_choice"] = desired
    _write_adapter_event(
        event_context,
        "required_subagent_tool_choice_set",
        upstream=upstream if isinstance(upstream, str) else None,
        tool_name=tool_name,
    )
    return True


def _function_tool_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        name
        for tool in value
        if isinstance(tool, Mapping)
        and tool.get("type") == "function"
        and isinstance((name := tool.get("name")), str)
    }


def _coerce_targets(value: Any) -> tuple[Any, bool]:
    return _semantic_coerce_targets(value)


def _coerce_target(value: Any) -> tuple[Any, bool]:
    return _semantic_coerce_target(value)


def _coerce_number(value: Any) -> tuple[Any, bool]:
    return _semantic_coerce_number(value)


def _codex_apps_flat_alias_parts(name: Any) -> tuple[str, str] | None:
    if not isinstance(name, str) or not name.startswith("mcp__codex_apps__"):
        return None
    local_gateway_namespace = "mcp__codex_apps__local_tool_gateway_"
    if name.startswith(local_gateway_namespace):
        tool_name = name[len(local_gateway_namespace) :].lstrip("_")
        if _valid_tool_name(tool_name):
            return local_gateway_namespace, tool_name
    namespace_stem, found, tool_name = name.rpartition("___")
    if not found:
        return None
    namespace = f"{namespace_stem}_"
    if (
        namespace.startswith("mcp__codex_apps__")
        and namespace.endswith("_")
        and _valid_tool_name(namespace)
        and _valid_tool_name(tool_name)
    ):
        return namespace, tool_name
    return None


def _codex_apps_flat_alias_name(name: Any) -> str | None:
    return name if _codex_apps_flat_alias_parts(name) is not None else None


def _split_namespace_tool_alias(name: Any) -> tuple[str, str] | None:
    if not isinstance(name, str):
        return None
    codex_apps_alias = _codex_apps_flat_alias_parts(name)
    if codex_apps_alias is not None:
        return codex_apps_alias
    for separator in ("__", "."):
        namespace, found, tool_name = name.rpartition(separator)
        if not found:
            continue
        if (
            _valid_tool_name(namespace)
            and _supports_explicit_namespace_alias(namespace)
            and _valid_tool_name(tool_name)
        ):
            return namespace, tool_name
    return None


def _codex_apps_namespace_flat_alias(namespace: Any, name: Any) -> str | None:
    if not (
        isinstance(namespace, str)
        and isinstance(name, str)
        and namespace.startswith("mcp__codex_apps__")
        and namespace.endswith("_")
        and _valid_tool_name(namespace)
        and _valid_tool_name(name)
    ):
        return None
    alias = f"{namespace}__{name}"
    return alias if _valid_tool_name(alias) else None


def _normalize_tool_search_arguments(value: Any) -> dict[str, Any] | None:
    return _semantic_normalize_tool_search_arguments(value)


def _bounded_empty_tool_search_terminal_calls(value: Any) -> dict[str, tuple[str, int]]:
    if not isinstance(value, list):
        return {}

    queries_by_call_id: dict[str, str] = {}
    empty_call_ids_by_query: dict[str, list[str]] = {}
    successful_queries: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        # A search-shaped item is client-owned only with the explicit
        # execution marker.  Missing/unknown ownership must not seed the
        # bounded-search ledger or later rewrite a provider lifecycle.
        if item.get("type") == "tool_search_call" and item.get("execution") == "client":
            arguments = _normalize_tool_search_arguments(item.get("arguments"))
            if arguments is None or _is_multi_agent_discovery_arguments(arguments):
                continue
            queries_by_call_id[call_id] = arguments["query"]
            continue
        if item.get("type") != "tool_search_output":
            continue
        query = queries_by_call_id.pop(call_id, None)
        tools = item.get("tools")
        if query is None or not isinstance(tools, list):
            continue
        if tools:
            successful_queries.add(query)
            continue
        empty_call_ids_by_query.setdefault(query, []).append(call_id)

    terminal_calls: dict[str, tuple[str, int]] = {}
    for query, call_ids in empty_call_ids_by_query.items():
        if query in successful_queries or len(call_ids) < TOOL_SEARCH_EMPTY_MISS_BOUND:
            continue
        terminal_calls[call_ids[TOOL_SEARCH_EMPTY_MISS_BOUND - 1]] = (
            query,
            TOOL_SEARCH_EMPTY_MISS_BOUND,
        )
    return terminal_calls


def _terminalize_bounded_empty_tool_search_misses(
    payload: dict[str, Any],
    terminal_calls: Mapping[str, tuple[str, int]],
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list) or not terminal_calls:
        return False

    rewritten_items: list[Any] = []
    changed = False
    for item in input_items:
        if (
            isinstance(item, Mapping)
            and item.get("type") == "tool_search_output"
            and isinstance(item.get("call_id"), str)
            and item["call_id"] in terminal_calls
        ):
            rewritten = dict(item)
            rewritten["status"] = TOOL_SEARCH_UNAVAILABLE_STATUS
            rewritten["query_classification"] = TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION
            _, count = terminal_calls[item["call_id"]]
            rewritten["empty_miss_count"] = count
            rewritten["terminal"] = True
            rewritten_items.append(rewritten)
            changed = True
            continue
        rewritten_items.append(item)
    if changed:
        payload["input"] = rewritten_items
    return changed


def _restrict_bounded_tool_search_queries(payload: dict[str, Any], bounded_queries: set[str]) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list) or not bounded_queries:
        return False

    restriction = {"enum": sorted(bounded_queries)}
    changed = False
    rewritten_tools: list[Any] = []
    for tool in tools:
        if not (
            isinstance(tool, Mapping)
            and tool.get("type") == "function"
            and tool.get("name") == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["name"]
        ):
            rewritten_tools.append(tool)
            continue
        rewritten_tool = dict(tool)
        parameters = dict(_tool_parameters_schema(tool))
        properties_value = parameters.get("properties")
        properties = dict(properties_value) if isinstance(properties_value, Mapping) else {}
        query_value = properties.get("query")
        query_schema = dict(query_value) if isinstance(query_value, Mapping) else {"type": "string"}
        if "not" in query_schema:
            query_schema = {"allOf": [query_schema, {"not": restriction}]}
        else:
            query_schema["not"] = restriction
        properties["query"] = query_schema
        parameters["properties"] = properties
        rewritten_tool["parameters"] = parameters
        rewritten_tools.append(rewritten_tool)
        changed = True
    if changed:
        payload["tools"] = rewritten_tools
    return changed


def _tool_search_query_digest(query: str) -> bytes:
    return hashlib.sha256(query.encode("utf-8")).digest()


def _bounded_tool_search_query_digests(event_context: Mapping[str, Any] | None) -> set[bytes]:
    value = (event_context or {}).get("_bounded_tool_search_query_digests")
    if not isinstance(value, (set, frozenset)):
        return set()
    return {digest for digest in value if isinstance(digest, bytes)}


def _tool_search_call_arguments(
    value: Mapping[str, Any],
    *,
    candidate_item_ids: set[str] | None = None,
    allow_legacy_function: bool = False,
) -> dict[str, Any] | None:
    # A provider may use the same item type for its own lifecycle.  An
    # explicit provider execution marker must never be treated as the
    # client-owned Codex search call that the bounded-miss guard can suppress.
    # The execution marker is part of the ownership contract.  Missing or
    # unknown values are not evidence that Codex owns the lifecycle; leave
    # those provider items untouched rather than letting the bounded-search
    # guard rewrite them.
    if value.get("type") == "tool_search_call" and value.get("execution") == "client":
        return _normalize_tool_search_arguments(value.get("arguments"))
    # A provider is allowed to expose an ordinary function named
    # ``tool_search``.  Treat the flattened spelling as client-owned only
    # after the stream has declared the item as a search candidate (or when a
    # legacy caller explicitly supplies the candidate set).  Otherwise the
    # bounded empty-search suppression would silently rewrite an unrelated
    # provider function with a matching ``query`` argument.
    if (
        value.get("type") == "function_call"
        and value.get("name") == "tool_search"
        and isinstance(value.get("id"), str)
        and value.get("id")
        and candidate_item_ids is not None
        and value.get("id") in candidate_item_ids
        and allow_legacy_function
    ):
        return _normalize_tool_search_arguments(value.get("arguments"))
    return None


def _bounded_tool_search_unavailable_message(item: Mapping[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {
                "type": "output_text",
                "text": (
                    "tool_search_unavailable\n"
                    f"query_classification: {TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION}\n"
                    f"empty_miss_count: {TOOL_SEARCH_EMPTY_MISS_BOUND}\n"
                    f"status: {TOOL_SEARCH_UNAVAILABLE_STATUS}\n"
                    "terminal: true\n"
                    "execution: suppressed"
                ),
            }
        ],
    }
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        message["id"] = item_id
    return message


def _suppress_bounded_tool_search_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    bounded_digests = _bounded_tool_search_query_digests(event_context)
    if not bounded_digests:
        return value, False

    if isinstance(event_context, dict):
        candidates_value = event_context.setdefault("_tool_search_stream_candidate_item_ids", set())
        candidate_item_ids = candidates_value if isinstance(candidates_value, set) else set()
        event_context["_tool_search_stream_candidate_item_ids"] = candidate_item_ids
        suppressed_value = event_context.setdefault("_bounded_tool_search_suppressed_item_ids", set())
        suppressed_item_ids = suppressed_value if isinstance(suppressed_value, set) else set()
        event_context["_bounded_tool_search_suppressed_item_ids"] = suppressed_item_ids
        allow_legacy_function = bool(event_context.get("_tool_search_client_owned"))
    else:
        candidate_item_ids = set()
        suppressed_item_ids = set()
        allow_legacy_function = False

    return _suppress_bounded_tool_search_calls_inner(
        value,
        bounded_digests,
        candidate_item_ids,
        suppressed_item_ids,
        allow_legacy_function,
    )


def _suppress_bounded_tool_search_calls_inner(
    value: Any,
    bounded_digests: set[bytes],
    candidate_item_ids: set[str],
    suppressed_item_ids: set[str],
    allow_legacy_function: bool,
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten_items: list[Any] = []
        for item in value:
            replacement, item_changed = _suppress_bounded_tool_search_calls_inner(
                item,
                bounded_digests,
                candidate_item_ids,
                suppressed_item_ids,
                allow_legacy_function,
            )
            if replacement is None:
                changed = True
                continue
            rewritten_items.append(replacement)
            changed = changed or item_changed
        return (rewritten_items if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    event_type = value.get("type")
    if event_type == "response.output_item.added":
        item = value.get("item")
        if isinstance(item, Mapping):
            item_id = item.get("id")
            if (
                isinstance(item_id, str)
                and item_id
                and (
                    (
                        item.get("type") == "tool_search_call"
                        and item.get("execution") == "client"
                    )
                    or (
                        allow_legacy_function
                        and item.get("type") == "function_call"
                        and item.get("name") == "tool_search"
                    )
                )
            ):
                candidate_item_ids.add(item_id)
    elif event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id in suppressed_item_ids:
            return None, True
        if (
            event_type == "response.function_call_arguments.done"
            and isinstance(item_id, str)
            and item_id in candidate_item_ids
        ):
            arguments = _normalize_tool_search_arguments(value.get("arguments"))
            if (
                arguments is not None
                and _tool_search_query_digest(arguments["query"]) in bounded_digests
            ):
                suppressed_item_ids.add(item_id)
                return None, True

    arguments = _tool_search_call_arguments(
        value,
        candidate_item_ids=candidate_item_ids,
        allow_legacy_function=allow_legacy_function,
    )
    if (
        arguments is not None
        and _tool_search_query_digest(arguments["query"]) in bounded_digests
    ):
        item_id = value.get("id")
        if isinstance(item_id, str) and item_id:
            suppressed_item_ids.add(item_id)
        return _bounded_tool_search_unavailable_message(value), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _suppress_bounded_tool_search_calls_inner(
            item,
            bounded_digests,
            candidate_item_ids,
            suppressed_item_ids,
            allow_legacy_function,
        )
        if replacement is None:
            rewritten.pop(key, None)
            changed = True
            continue
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _is_multi_agent_discovery_arguments(arguments: Mapping[str, Any] | None) -> bool:
    if not arguments:
        return False
    query = arguments.get("query")
    if not isinstance(query, str):
        return False
    lowered = query.lower()
    return all(term in lowered for term in ("spawn_agent", "multi_agent", "subagent"))


def _multi_agent_discovery_arguments(value: Any) -> dict[str, Any] | None:
    return _semantic_multi_agent_discovery_arguments(value)


def _normalize_multi_agent_arguments(
    value: Any,
    tool_name: str | None,
) -> tuple[Any, str | None, bool]:
    return _semantic_normalize_multi_agent_arguments(value, tool_name)


def _raise_worker_contract_error(
    *,
    event: str,
    error_code: str,
    classification: str,
    surface: str | None = None,
) -> None:
    fields = {
        "outcome": "rejected",
        "classification": classification,
    }
    if surface is not None:
        fields["surface"] = surface
    write_proxy_event(event, **fields)
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            error_code,
            "External Worker delegation contract validation failed.",
        )
    )


def _validate_external_worker_selectors(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_external_worker_selectors(item, event_context, surface=surface)
        return
    if not isinstance(value, Mapping):
        return

    if _multi_agent_function_call_name(value) == "spawn_agent":
        raw_arguments = value.get("arguments")
        arguments = _json_object_from_arguments(raw_arguments)
        if arguments is not None and raw_arguments not in (None, ""):
            agent_type = arguments.get("agent_type")
            if agent_type == "general":
                pass
            elif agent_type == "worker":
                if not _worker_caller_carrier_supported(event_context):
                    _raise_worker_contract_error(
                        event="worker_selector_validated",
                        error_code=WORKER_SELECTOR_ERROR_CODE,
                        classification="unsupported_caller_carrier",
                        surface=surface,
                    )
                write_proxy_event(
                    "worker_selector_validated",
                    outcome="accepted",
                    classification="worker_preserved",
                    surface=surface,
                )
            elif agent_type is not None or bool((event_context or {}).get("_spawn_selector_required")):
                validation = _semantic_validate_worker_selector(arguments)
                _raise_worker_contract_error(
                    event="worker_selector_validated",
                    error_code=WORKER_SELECTOR_ERROR_CODE,
                    classification=validation.classification,
                    surface=surface,
                )

    for item in value.values():
        _validate_external_worker_selectors(item, event_context, surface=surface)


def _worker_caller_carrier_supported(event_context: Mapping[str, Any] | None) -> bool:
    context = event_context or {}
    caller_format = context.get("_caller_wire_format", context.get("inbound_format", "responses"))
    return caller_format != "chat_completions"


def _requested_reasoning_effort(payload: Mapping[str, Any]) -> Any:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, Mapping):
        return reasoning.get("effort")
    if isinstance(reasoning, str):
        return reasoning
    return payload.get("reasoning_effort")


def _worker_requested_binding_signature_payload(binding: Mapping[str, Any], call_id: str) -> bytes:
    signed_binding = {
        "contract_version": binding.get("contract_version"),
        "agent_type": binding.get("agent_type"),
        "model": binding.get("model"),
        "reasoning": binding.get("reasoning"),
    }
    canonical = json.dumps(signed_binding, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return call_id.encode("utf-8") + b"\0" + canonical


def _requested_worker_binding_signature(binding: Mapping[str, Any], call_id: str) -> str:
    return worker_binding_signing.sign(
        WORKER_BINDING_SIGNING_ROOT,
        _worker_requested_binding_signature_payload(binding, call_id),
    )


def _worker_requested_binding_sidecar(
    requested: Mapping[str, Any],
    call_id: str,
) -> dict[str, Any]:
    validation = _semantic_validate_requested_worker_binding(requested)
    if validation.outcome != _BINDING_ACCEPTED:
        _raise_worker_contract_error(
            event="worker_requested_binding_validated",
            error_code=WORKER_BINDING_ERROR_CODE,
            classification=validation.classification,
        )
    binding = {
        "contract_version": WORKER_REQUESTED_BINDING_VERSION,
        "agent_type": requested["agent_type"],
        "model": requested["model"],
        "reasoning": requested["reasoning"],
    }
    return {**binding, "signature": _requested_worker_binding_signature(binding, call_id)}


def _verified_worker_requested_binding(
    value: Any,
    call_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return None, "missing_requested_binding_sidecar"
    if not isinstance(value, Mapping) or set(value) != WORKER_REQUESTED_BINDING_FIELDS:
        return None, "unknown_requested_binding_sidecar"
    if value.get("contract_version") != WORKER_REQUESTED_BINDING_VERSION:
        return None, "unknown_requested_binding_sidecar"
    signature = value.get("signature")
    binding = {
        "contract_version": value.get("contract_version"),
        "agent_type": value.get("agent_type"),
        "model": value.get("model"),
        "reasoning": value.get("reasoning"),
    }
    if not worker_binding_signing.verify(
        WORKER_BINDING_SIGNING_ROOT,
        _worker_requested_binding_signature_payload(binding, call_id),
        signature,
    ):
        return None, "unknown_requested_binding_sidecar"
    requested = {
        "agent_type": binding["agent_type"],
        "model": binding["model"],
        "reasoning": binding["reasoning"],
    }
    validation = _semantic_validate_requested_worker_binding(requested)
    if validation.outcome != _BINDING_ACCEPTED:
        return None, validation.classification
    return requested, None


_WORKER_STREAM_BINDING_STATE_FIELD = "_worker_stream_binding_state"


def _remember_worker_stream_item(
    state: dict[str, Any],
    item: Any,
    *,
    terminal: bool = False,
) -> None:
    if not isinstance(item, Mapping):
        return
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return
    tool_name = _multi_agent_function_call_name(item)
    items = state.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        state["items"] = items
    record = items.setdefault(item_id, {})
    if not isinstance(record, dict):
        record = {}
        items[item_id] = record
    if tool_name is not None:
        record["tool_name"] = tool_name
    if tool_name != "spawn_agent":
        return
    raw_arguments = item.get("arguments")
    if raw_arguments not in (None, ""):
        record["selector_arguments_pending"] = False
        if isinstance(raw_arguments, str):
            record["arguments"] = raw_arguments
        elif isinstance(raw_arguments, Mapping):
            record["arguments"] = json.dumps(raw_arguments, ensure_ascii=True, separators=(",", ":"))
        parsed = _semantic_strict_json_object(record.get("arguments"))
        if parsed is not None and isinstance(parsed.get("agent_type"), str):
            if not record.get("selector_invalid"):
                record["selector_delta_incomplete"] = False
                record["agent_type"] = parsed["agent_type"]
        elif terminal:
            record["selector_invalid"] = True
            record.pop("agent_type", None)
        else:
            record["selector_delta_incomplete"] = True
            record.pop("agent_type", None)
    else:
        if not terminal:
            record["selector_arguments_pending"] = True
        elif record.get("selector_arguments_pending") and not record.get("selector_arguments_done"):
            record["selector_invalid"] = True
            record.pop("agent_type", None)


def _remember_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> None:
    if not isinstance(event_context, dict):
        return
    state = event_context.get(_WORKER_STREAM_BINDING_STATE_FIELD)
    if not isinstance(state, dict):
        state = {"items": {}}
        event_context[_WORKER_STREAM_BINDING_STATE_FIELD] = state
    event_type = value.get("type")
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        _remember_worker_stream_item(
            state,
            value.get("item"),
            terminal=event_type == "response.output_item.done",
        )
        return
    if event_type == "response.function_call_arguments.delta":
        item_id = value.get("item_id")
        delta = value.get("delta")
        if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
            return
        items = state.setdefault("items", {})
        if not isinstance(items, dict):
            return
        record = items.setdefault(item_id, {})
        if not isinstance(record, dict):
            record = {}
            items[item_id] = record
        record["arguments"] = f"{record.get('arguments', '')}{delta}"
        record["selector_arguments_pending"] = True
        parsed = _semantic_strict_json_object(record["arguments"])
        if parsed is not None and isinstance(parsed.get("agent_type"), str):
            if not record.get("selector_invalid"):
                record["selector_delta_incomplete"] = False
                record["agent_type"] = parsed["agent_type"]
        else:
            record["selector_delta_incomplete"] = True
            record.pop("agent_type", None)
        return
    if event_type == "response.function_call_arguments.done":
        item_id = value.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            return
        items = state.setdefault("items", {})
        if not isinstance(items, dict):
            return
        record = items.setdefault(item_id, {})
        if not isinstance(record, dict):
            record = {}
            items[item_id] = record
        arguments = value.get("arguments")
        if isinstance(arguments, str):
            record["arguments"] = arguments
            if record.get("tool_name") != "spawn_agent":
                return
            record["selector_arguments_done"] = True
            record["selector_arguments_pending"] = False
            parsed = _semantic_strict_json_object(arguments)
            if parsed is not None and isinstance(parsed.get("agent_type"), str):
                if not record.get("selector_invalid"):
                    record["selector_delta_incomplete"] = False
                    record["agent_type"] = parsed["agent_type"]
            else:
                record["selector_invalid"] = True
                record.pop("agent_type", None)
        return
    if event_type == "response.completed":
        response = value.get("response")
        output = response.get("output") if isinstance(response, Mapping) else None
        if isinstance(output, list):
            for item in output:
                _remember_worker_stream_item(state, item, terminal=True)


def _raise_on_invalid_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    """Reject a terminal streamed worker call before any semantic repair."""
    if _is_collaboration_v2_context(event_context):
        return
    context = event_context or {}
    if not context.get("_worker_binding_required"):
        return
    state = context.get(_WORKER_STREAM_BINDING_STATE_FIELD)
    items = state.get("items") if isinstance(state, Mapping) else None
    if not isinstance(items, Mapping):
        return

    event_type = value.get("type")
    item_ids: list[str] = []
    if event_type == "response.function_call_arguments.done":
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id:
            item_ids.append(item_id)
    elif event_type == "response.output_item.done":
        item = value.get("item")
        item_id = item.get("id") if isinstance(item, Mapping) else None
        if isinstance(item_id, str) and item_id:
            item_ids.append(item_id)
    elif event_type == "response.completed":
        response = value.get("response")
        output = response.get("output") if isinstance(response, Mapping) else None
        if isinstance(output, list):
            item_ids.extend(
                item["id"]
                for item in output
                if isinstance(item, Mapping)
                and isinstance(item.get("id"), str)
                and item.get("id")
            )

    for item_id in item_ids:
        record = items.get(item_id)
        if (
            isinstance(record, Mapping)
            and record.get("tool_name") == "spawn_agent"
            and record.get("selector_invalid")
        ):
            _raise_worker_contract_error(
                event="worker_selector_validated",
                error_code=WORKER_SELECTOR_ERROR_CODE,
                classification="malformed_arguments",
                surface=surface,
            )


def _attach_worker_requested_binding_sidecars(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    capture_stream_event: bool = True,
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _attach_worker_requested_binding_sidecars(
                item,
                event_context,
                capture_stream_event=capture_stream_event,
            )
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed
    if not isinstance(value, dict):
        return value, False

    if capture_stream_event:
        _remember_worker_stream_event(value, event_context)
    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _attach_worker_requested_binding_sidecars(
            item,
            event_context,
            capture_stream_event=capture_stream_event,
        )
        if item_changed:
            rewritten[key] = replacement
            changed = True

    if _multi_agent_function_call_name(rewritten) != "spawn_agent":
        return (rewritten if changed else value), changed
    # Binding sidecars require an exact selector.  The general argument
    # normalizer intentionally accepts a valid JSON prefix for other repair
    # paths, but that would let malformed streamed arguments inherit a worker
    # binding after the strict stream state has already been cleared.
    arguments = _semantic_strict_json_object(rewritten.get("arguments"))
    context = event_context or {}
    pending_agent_type = None
    stream_item_tracked = False
    stream_selector_invalid = False
    stream_state = context.get(_WORKER_STREAM_BINDING_STATE_FIELD)
    item_id = rewritten.get("id")
    if isinstance(stream_state, Mapping) and isinstance(item_id, str):
        stream_items = stream_state.get("items")
        record = stream_items.get(item_id) if isinstance(stream_items, Mapping) else None
        if isinstance(record, Mapping):
            stream_item_tracked = True
            pending_agent_type = record.get("agent_type")
            stream_selector_invalid = bool(record.get("selector_invalid"))
    if arguments is None:
        # Responses streams may publish the function-call item before its
        # arguments.  The arguments delta/done events carry the selector, but
        # the item that Codex persists can still have an empty arguments field.
        # When this request has an external worker binding, carry the signed
        # sidecar on that item so the next turn can validate the reconstructed
        # worker call.  A normal body call has no lifecycle status and keeps the
        # old fail-closed behavior.
        if not (
            rewritten.get("arguments") in (None, "")
            and bool(context.get("_worker_binding_required"))
            and rewritten.get("status") in {"in_progress", "completed"}
            and pending_agent_type == "worker"
        ):
            return (rewritten if changed else value), changed
    elif arguments.get("agent_type") != "worker":
        return (rewritten if changed else value), changed
    elif stream_item_tracked and (
        stream_selector_invalid or pending_agent_type not in {None, "worker"}
    ):
        return (rewritten if changed else value), changed
    call_id = rewritten.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        _raise_worker_contract_error(
            event="worker_requested_binding_validated",
            error_code=WORKER_BINDING_ERROR_CODE,
            classification="missing_call_identity",
        )
    requested = (event_context or {}).get("_worker_requested_binding")
    if not isinstance(requested, Mapping):
        _raise_worker_contract_error(
            event="worker_requested_binding_validated",
            error_code=WORKER_BINDING_ERROR_CODE,
            classification="missing_requested_binding_sidecar",
        )
    sidecar = _worker_requested_binding_sidecar(requested, call_id)
    if rewritten.get(WORKER_REQUESTED_BINDING_FIELD) != sidecar:
        rewritten[WORKER_REQUESTED_BINDING_FIELD] = sidecar
        changed = True
    return (rewritten if changed else value), changed


def _apply_external_worker_response_contract(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    validate_selectors: bool = True,
    attach_sidecars: bool = True,
    capture_stream_event: bool = True,
) -> tuple[Any, bool]:
    if _is_collaboration_v2_context(event_context):
        return value, False
    if validate_selectors:
        _validate_external_worker_selectors(value, event_context, surface=surface)
    if attach_sidecars:
        return _attach_worker_requested_binding_sidecars(
            value,
            event_context,
            capture_stream_event=capture_stream_event,
        )
    return value, False


def _validate_worker_binding_history(
    payload: Mapping[str, Any],
) -> None:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return

    worker_calls: dict[str, Mapping[str, Any]] = {}
    validated_call_ids: set[str] = set()
    for item in input_items:
        if not isinstance(item, Mapping):
            continue
        call_id = item.get("call_id")
        if item.get("type") == "function_call" and _multi_agent_function_call_name(item) == "spawn_agent":
            arguments = _json_object_from_arguments(item.get("arguments"))
            agent_type = arguments.get("agent_type") if arguments is not None else None
            if agent_type == "general":
                continue
            selector_validation = _semantic_validate_worker_selector(arguments)
            if selector_validation.outcome != _BINDING_ACCEPTED:
                _raise_worker_contract_error(
                    event="worker_selector_validated",
                    error_code=WORKER_SELECTOR_ERROR_CODE,
                    classification=selector_validation.classification,
                    surface="history",
                )
            if not isinstance(call_id, str) or not call_id:
                _raise_worker_contract_error(
                    event="worker_effective_binding_validated",
                    error_code=WORKER_BINDING_ERROR_CODE,
                    classification="missing_call_identity",
                )
            if call_id in worker_calls:
                _raise_worker_contract_error(
                    event="worker_effective_binding_validated",
                    error_code=WORKER_BINDING_ERROR_CODE,
                    classification="duplicate_worker_call_identity",
                )
            requested, sidecar_failure = _verified_worker_requested_binding(
                item.get(WORKER_REQUESTED_BINDING_FIELD),
                call_id,
            )
            if requested is None:
                _raise_worker_contract_error(
                    event="worker_requested_binding_validated",
                    error_code=WORKER_BINDING_ERROR_CODE,
                    classification=sidecar_failure or "unknown_requested_binding_sidecar",
                )
            if isinstance(item, dict):
                item.pop(WORKER_REQUESTED_BINDING_FIELD, None)
            worker_calls[call_id] = requested
            continue
        if (
            item.get("type") != "function_call_output"
            or not isinstance(call_id, str)
            or call_id not in worker_calls
        ):
            continue
        if call_id in validated_call_ids:
            _raise_worker_contract_error(
                event="worker_effective_binding_validated",
                error_code=WORKER_BINDING_ERROR_CODE,
                classification="duplicate_worker_effective_output",
            )

        output = item.get("output")
        readback = _semantic_strict_json_object(output)
        if readback is None and isinstance(output, str) and output.strip():
            _raise_worker_contract_error(
                event="worker_effective_binding_validated",
                error_code=WORKER_BINDING_ERROR_CODE,
                classification="malformed_readback",
            )
        requested = worker_calls[call_id]
        validation = _semantic_validate_effective_worker_binding(
            requested,
            readback,
        )
        if validation.outcome != _BINDING_ACCEPTED:
            _raise_worker_contract_error(
                event="worker_effective_binding_validated",
                error_code=WORKER_BINDING_ERROR_CODE,
                classification=validation.classification,
            )
        write_proxy_event(
            "worker_effective_binding_validated",
            outcome="accepted",
            classification=validation.classification,
        )
        validated_call_ids.add(call_id)

    if set(worker_calls) - validated_call_ids:
        _raise_worker_contract_error(
            event="worker_effective_binding_validated",
            error_code=WORKER_BINDING_ERROR_CODE,
            classification="missing_readback",
        )


def _normalize_third_party_tool_call(
    value: Any,
    event_context: Mapping[str, Any] | None = None,
) -> tuple[Any, bool]:
    if _is_collaboration_v2_context(event_context):
        return value, False
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _normalize_third_party_tool_call(item, event_context)
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    changed = False
    rewritten = dict(value)
    if (
        value.get("type") == "function_call"
        and value.get("name") == "tool_search"
        and bool((event_context or {}).get("_tool_search_client_owned"))
    ):
        arguments = _normalize_tool_search_arguments(value.get("arguments"))
        if arguments is not None:
            rewritten["type"] = "tool_search_call"
            rewritten["arguments"] = arguments
            rewritten.pop("name", None)
            rewritten.setdefault("execution", "client")
            rewritten.setdefault("status", "completed")
            changed = True
    elif (
        value.get("type") == "function_call"
        and value.get("name") in MULTI_AGENT_NAMESPACE_ALIASES
        and _multi_agent_discovery_arguments(value.get("arguments")) is not None
    ):
        arguments = _multi_agent_discovery_arguments(value.get("arguments"))
        rewritten["type"] = "tool_search_call"
        rewritten["arguments"] = arguments
        rewritten.pop("name", None)
        rewritten.setdefault("execution", "client")
        rewritten.setdefault("status", "completed")
        changed = True
    elif _is_tool_call_item(value):
        original_name = value.get("name")
        tool_name = _multi_agent_alias_tool_name(original_name)
        namespace_alias = None
        argument_key = "arguments" if "arguments" in value else "input" if "input" in value else None
        if (
            argument_key is not None
            and not (
                value.get("type") == "custom_tool_call"
                and original_name == APPLY_PATCH_FUNCTION_NAME
            )
            and _json_argument_string_needs_repair(value.get(argument_key))
        ):
            repaired_arguments = _json_object_from_arguments(value.get(argument_key))
            if repaired_arguments is not None:
                rewritten[argument_key] = _dump_arguments_like(value.get(argument_key), repaired_arguments)
                changed = True
        if (
            (value.get("namespace") == NODE_REPL_NAMESPACE and original_name == "js")
            or original_name in {f"{NODE_REPL_NAMESPACE}.js", f"{NODE_REPL_NAMESPACE}__js"}
        ):
            rewritten["namespace"] = NODE_REPL_NAMESPACE
            rewritten["name"] = "js"
            changed = True
        elif tool_name is None:
            namespace_alias = _split_namespace_tool_alias(original_name)
        if original_name in MULTI_AGENT_NAMESPACE_ALIASES and argument_key is not None:
            normalized, tool_name, args_changed = _normalize_multi_agent_arguments(rewritten.get(argument_key), None)
            if args_changed:
                rewritten[argument_key] = normalized
                changed = True
        elif tool_name is not None and argument_key is not None:
            normalized, _, args_changed = _normalize_multi_agent_arguments(rewritten.get(argument_key), tool_name)
            if args_changed:
                rewritten[argument_key] = normalized
                changed = True

        if tool_name is not None:
            rewritten["name"] = tool_name
            rewritten["namespace"] = "multi_agent_v1"
            changed = True
        elif namespace_alias is not None:
            namespace_name, namespaced_tool_name = namespace_alias
            rewritten["name"] = namespaced_tool_name
            rewritten["namespace"] = namespace_name
            changed = True
    else:
        original_name = value.get("name")
        tool_name = _multi_agent_alias_tool_name(original_name)
        if tool_name is not None and _looks_like_response_tool_name_fragment(value):
            rewritten["name"] = tool_name
            rewritten["namespace"] = "multi_agent_v1"
            changed = True

    for key, item in list(rewritten.items()):
        replacement, item_changed = _normalize_third_party_tool_call(item, event_context)
        if item_changed:
            rewritten[key] = replacement
            changed = True

    return (rewritten if changed else value), changed


def _compatible_multi_agent_call_message(item: Mapping[str, Any], tool_name: str) -> dict[str, str]:
    lines = [f"Previous real Codex native multi_agent_v1.{tool_name} call transcript"]
    value = _stringify_internal_field(item.get("call_id"))
    if value:
        lines.append(f"call_id: {value}")
    _append_internal_field(lines, "arguments", item.get("arguments"))
    return _developer_text_message("\n".join(lines))


def _status_completed_agent_ids(status: Any) -> list[str]:
    if not isinstance(status, Mapping):
        return []
    return [
        agent_id
        for agent_id, value in status.items()
        if isinstance(agent_id, str) and isinstance(value, Mapping) and "completed" in value
    ]


def _status_not_found_agent_ids(status: Any) -> list[str]:
    if not isinstance(status, Mapping):
        return []
    return [
        agent_id
        for agent_id, value in status.items()
        if isinstance(agent_id, str) and isinstance(value, str) and value == "not_found"
    ]


def _has_multi_agent_discovery_tools(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("type") == "namespace"
        and item.get("name") == "multi_agent_v1"
        for item in value
    )


def _text_contains_multi_agent_discovery(value: Any) -> bool:
    if isinstance(value, str):
        return "discovered_codex_native_multi_agent_tools" in value
    if isinstance(value, Mapping):
        return any(_text_contains_multi_agent_discovery(child) for child in value.values())
    if isinstance(value, list):
        return any(_text_contains_multi_agent_discovery(child) for child in value)
    return False


def _has_multi_agent_discovery_context(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "tool_search_output" and _has_multi_agent_discovery_tools(item.get("tools")):
            return True
        if item.get("type") == "message" and _text_contains_multi_agent_discovery(item.get("content")):
            return True
    return False


def _joined_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_joined_text(child) for child in value.values())
    if isinstance(value, list):
        return "\n".join(_joined_text(child) for child in value)
    return ""


def _active_user_request_text(value: Any) -> str:
    if not isinstance(value, list):
        return _joined_text(value)
    for item in reversed(value):
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        if item.get("role") != "user":
            continue
        text = _joined_text(item.get("content"))
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line.startswith("Previous real Codex native ") or first_line.startswith("Codex native "):
            continue
        if text.strip():
            return text
    return ""


def _exact_child_prompts_from_request_text(text: str) -> list[str]:
    prompts: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"child prompt must be exactly this complete string:\s*`([^`]+)`",
        r"Spawn child [A-Z]\s+with prompt exactly this complete string:\s*`([^`]+)`",
        r"Spawn child [A-Z]\s+with prompt exactly:\s*([^\r\n]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            prompt = match.group(1).strip()
            if prompt and prompt not in seen:
                prompts.append(prompt)
                seen.add(prompt)
    return prompts


def _required_spawn_arguments_for_state(input_items: Any, subagent_state: Any | None) -> dict[str, Any] | None:
    if subagent_state is None or getattr(subagent_state, "next_action", None) != "spawn":
        return None
    text = _active_user_request_text(input_items)
    prompts = _exact_child_prompts_from_request_text(text)
    if not prompts:
        return _required_workflow_spawn_arguments(text, subagent_state)
    index = len(getattr(subagent_state, "agents", {}) or {})
    if index >= len(prompts):
        return None
    prompt = prompts[index]
    if not prompt:
        return None
    return {"message": prompt, "fork_context": False}


def _required_workflow_spawn_arguments(text: str, subagent_state: Any) -> dict[str, Any] | None:
    if not bool(getattr(subagent_state, "workflow_intent", False)):
        return None
    if not bool(getattr(subagent_state, "workflow_plan_read", False)):
        return None
    role = getattr(subagent_state, "next_expected_role", None)
    if role not in {"implementer", "spec_reviewer", "code_quality_reviewer"}:
        return None

    output_path = _line_value(text, "OUTPUT_PATH=")
    sentinel = _line_value(text, "SENTINEL=")
    model = _line_value(text, "MODEL_UNDER_TEST=") or _line_value(text, "MODEL=")
    endpoint = _line_value(text, "ENDPOINT_UNDER_TEST=") or _line_value(text, "ENDPOINT=")
    case_name = _line_value(text, "CASE=")
    if not all(isinstance(value, str) and value for value in (output_path, sentinel, model, endpoint, case_name)):
        return None

    baseline_status = _workflow_baseline_status(text)
    artifact_text = "\n".join(
        [
            f"case: {case_name}",
            f"model: {model}",
            f"endpoint: {endpoint}",
            sentinel,
            "artifact: ok",
        ]
    )
    run_dir = str(Path(output_path).parent)
    if role == "implementer":
        message = f"""You are the IMPLEMENTER subagent in a Subagent-Driven Development workflow.

Your job is the single, minimal task described below. Do exactly this and nothing else.

Create exactly one UTF-8 text artifact at this absolute path:
  OUTPUT_PATH = {output_path}

Required file content, exactly five lines plus a trailing newline:
{artifact_text}

Hard constraints:
1. Create exactly one file: OUTPUT_PATH above.
2. Do not modify product-source files and do not commit anything.
3. Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.
4. After writing, read the file back and confirm it matches the required content exactly.

Report back with only:
Status: DONE
Artifact path: {output_path}
Bytes written: <integer>
File ends with newline: <yes/no>
Other files created: <none, list if any>
"""
        return {"message": message, "nickname": "implementer", "fork_context": False}

    if role == "spec_reviewer":
        message = f"""You are the SPEC REVIEWER subagent in a Subagent-Driven Development workflow.

Your single job is to verify the diagnostic artifact matches its specification exactly. Do not modify or create files.

Artifact path:
  {output_path}

Required file content, exactly five lines plus a trailing newline:
{artifact_text}

Verification steps:
1. Read the artifact using native shell/file-read tools.
2. Confirm the file exists, is UTF-8 text, and ends with a trailing newline.
3. Confirm all five lines above are present in exact order with no extra content.
4. Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.

Report back with only:
Verdict: PASS | FAIL
Checks: <one-line summary>
Failures: <none, or specific failures>
"""
        return {"message": message, "nickname": "spec-reviewer", "fork_context": False}

    message = f"""You are the CODE-QUALITY REVIEWER subagent in a Subagent-Driven Development workflow.

Your single job is to verify the implementer's work is minimal. Do not modify or create files.

Expected artifact:
  {output_path}

Coordinator-owned scaffolding to ignore:
  {run_dir}

Baseline git status entries allowed for this case:
```text
{baseline_status or "<none>"}
```
These baseline entries are pre-existing coordinator-owned changes. Do not report baseline-listed paths as product-source modifications introduced by the implementer.

Verification steps:
1. Run git status --porcelain=v1 -uall.
2. Confirm the expected artifact exists and is non-empty.
3. Ignore coordinator-owned files under the scaffolding path above.
4. Fail only for implementer-owned extra files or product-source modifications not listed in the baseline block above.
5. Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.

Report back with only:
Verdict: PASS | FAIL
Artifact present: <yes/no>
Product-source modifications introduced: <none, or paths>
Extra implementer-owned files: <none, or paths>
Runner-owned scaffolding files observed: <short summary>
"""
    return {"message": message, "nickname": "quality-reviewer", "fork_context": False}


def _workflow_baseline_status(text: str) -> str:
    marker = "Baseline git status before this E2E case started"
    marker_index = text.lower().find(marker.lower())
    if marker_index < 0:
        return ""
    candidate = text[marker_index:]
    match = re.search(r"```(?:text)?\s*\n(?P<body>.*?)```", candidate, re.DOTALL)
    if match:
        return match.group("body").strip()
    return ""


def _line_value(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def _multi_agent_result_text(item: Mapping[str, Any], tool_name: str) -> str | None:
    if item.get("type") != "message":
        return None
    text = _joined_text(item.get("content"))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = f"Codex native multi_agent_v1.{tool_name} result"
    if not lines or lines[0] != header:
        return None
    return "\n".join(lines)


def _open_multi_agent_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    open_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        spawn_text = _multi_agent_result_text(item, "spawn_agent")
        if spawn_text is not None and "status: succeeded" in spawn_text:
            agent_id = _line_value(spawn_text, "agent_id:")
            if agent_id:
                open_agent_ids.add(agent_id)
        close_text = _multi_agent_result_text(item, "close_agent")
        if close_text is not None and "status: closed" in close_text:
            closed_agent_id = _line_value(close_text, "closed_agent_id:")
            if closed_agent_id:
                open_agent_ids.discard(closed_agent_id)
            else:
                open_agent_ids.clear()
    return sorted(open_agent_ids)


def _spawned_multi_agent_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    spawned_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        spawn_text = _multi_agent_result_text(item, "spawn_agent")
        if spawn_text is not None and "status: succeeded" in spawn_text:
            agent_id = _line_value(spawn_text, "agent_id:")
            if agent_id:
                spawned_agent_ids.add(agent_id)
    return sorted(spawned_agent_ids)


def _split_agent_id_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def _completed_multi_agent_wait_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    completed_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = _multi_agent_result_text(item, "wait_agent")
        if text is None or "status: completed" not in text:
            continue
        for agent_id in _split_agent_id_list(_line_value(text, "completed_agent_ids:")):
            completed_agent_ids.add(agent_id)
    return sorted(completed_agent_ids)


def _closed_multi_agent_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    closed_agent_ids: set[str] = set()
    closed_unknown = False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = _multi_agent_result_text(item, "close_agent")
        if text is None or "status: closed" not in text:
            continue
        closed_agent_id = _line_value(text, "closed_agent_id:")
        if closed_agent_id:
            closed_agent_ids.add(closed_agent_id)
        else:
            closed_unknown = True
    if closed_unknown and not closed_agent_ids:
        return ["<unknown>"]
    return sorted(closed_agent_ids)


def _has_single_loop_multi_agent_request(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    text = _joined_text(value).lower()
    if not any(token in text for token in ("spawn_agent", "multi_agent", "subagent", "子代理")):
        return False
    return any(
        token in text
        for token in (
            "只执行一次",
            "执行一次真实",
            "一次真实",
            "一个子代理",
            "最终回复",
            "不要再 spawn",
            "不要重复验证",
            "不要重复",
            "only once",
            "single spawn",
            "single loop",
            "single lifecycle",
            "exactly one",
            "one lifecycle",
            "do not spawn again",
            "don't spawn again",
            "do not repeat",
        )
    )


def _requested_multi_agent_spawn_count(value: Any) -> int | None:
    if not isinstance(value, list):
        return None
    text = _joined_text(value).lower()
    if not any(token in text for token in ("spawn_agent", "multi_agent", "subagent", "子代理")):
        return None

    for pattern in (
        r"(?:spawn|spawns|创建|启动|派发|调用|开|生成)\s*(?<!第)(\d{1,2})\s*(?:个|名|位)?\s*(?:subagents?|agents?|子代理)",
        r"(?<!第)(\d{1,2})\s*(?:个|名|位)?\s*(?:subagents?|agents?|子代理)",
    ):
        match = re.search(pattern, text)
        if match:
            count = int(match.group(1))
            return count if 0 < count <= 20 else None

    chinese_numbers = {
        "一个": 1,
        "一": 1,
        "两个": 2,
        "两": 2,
        "二个": 2,
        "二": 2,
        "三个": 3,
        "三": 3,
        "四个": 4,
        "四": 4,
        "五个": 5,
        "五": 5,
        "六个": 6,
        "六": 6,
        "七个": 7,
        "七": 7,
        "八个": 8,
        "八": 8,
        "九个": 9,
        "九": 9,
        "十个": 10,
        "十": 10,
    }
    chinese_pattern = "|".join(sorted((re.escape(key) for key in chinese_numbers), key=len, reverse=True))
    match = re.search(rf"(?<!第)({chinese_pattern})\s*(?:subagents?|agents?|子代理)", text)
    if match:
        return chinese_numbers[match.group(1)]
    return None


def _has_single_step_node_repl_request(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    text = _joined_text(value).lower()
    if not any(token in text for token in ("mcp__node_repl", "node_repl")):
        return False
    return any(
        token in text
        for token in (
            "exactly once",
            "one tool result",
            "stop tool use",
            "single-step",
            "single step",
            "只调用一次",
            "只执行一次",
            "不要重复",
        )
    )


def _has_completed_single_step_node_repl_context(value: Any) -> bool:
    if _has_browser_context_signal(value) or not _has_single_step_node_repl_request(value):
        return False
    text = _joined_text(value).lower()
    return "codex native mcp__node_repl.js result" in text and "status: completed" in text


def _looks_like_subagent_workflow_plan_text(text: str) -> bool:
    lowered = text.lower()
    if "# short subagent development e2e plan" in lowered:
        return True
    return (
        "output_path" in lowered
        and "sentinel" in lowered
        and "implementer" in lowered
        and ("spec reviewer" in lowered or "spec compliance" in lowered)
        and ("quality reviewer" in lowered or "code quality" in lowered)
    )


def _has_node_repl_subagent_plan_read_context(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    node_repl_call_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call" and isinstance(call_id, str):
            if _node_repl_function_call_name(item) is not None:
                node_repl_call_ids.add(call_id)
            continue
        if item_type == "function_call_output" and isinstance(call_id, str) and call_id in node_repl_call_ids:
            if _looks_like_subagent_workflow_plan_text(_joined_text(item.get("output"))):
                return True
            continue
        if item_type == "message":
            text = _joined_text(item.get("content"))
            if "codex native mcp__node_repl.js result" in text.lower() and _looks_like_subagent_workflow_plan_text(text):
                return True
    return False


def _node_repl_single_step_complete_message() -> dict[str, str]:
    return _developer_text_message(
        "\n".join(
            [
                "Codex native mcp__node_repl.js current state",
                "status: single_step_complete",
                "completed_tool_alias: mcp__node_repl__js",
                "completed_native_tool: mcp__node_repl.js",
                "required_next_action: write the final answer now. The node_repl tool call already completed successfully; do not infer hidden tools were unavailable, and do not call mcp__node_repl__js, mcp__node_repl.js, or tool_search again for this single-step request.",
            ]
        )
    )


def _has_completed_single_loop_multi_agent_context(value: Any) -> bool:
    return _has_single_loop_multi_agent_request(value) and bool(_closed_multi_agent_ids(value)) and not _has_open_multi_agent_context(value)


def _has_open_multi_agent_context(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    if _open_multi_agent_ids(value):
        return True
    unknown_open_agent = False
    for item in value:
        if not isinstance(item, Mapping):
            continue
        spawn_text = _multi_agent_result_text(item, "spawn_agent")
        if spawn_text is not None and "status: succeeded" in spawn_text:
            if not _line_value(spawn_text, "agent_id:"):
                unknown_open_agent = True
        close_text = _multi_agent_result_text(item, "close_agent")
        if close_text is not None and "status: closed" in close_text:
            if not _line_value(close_text, "closed_agent_id:"):
                unknown_open_agent = False
    return unknown_open_agent


def _multi_agent_lifecycle_complete_message(closed_agent_ids: list[str]) -> dict[str, str]:
    lines = ["Codex native multi_agent_v1 current state"]
    lines.append("status: lifecycle_complete")
    if closed_agent_ids:
        lines.append(f"closed_agent_ids: {', '.join(closed_agent_ids)}")
    lines.append("completed_tool_aliases: multi_agent_v1__spawn_agent, multi_agent_v1__wait_agent, multi_agent_v1__close_agent")
    lines.append(
        "visible_response_required: emit the final report as ordinary assistant message content, not only reasoning, analysis, hidden notes, or tool arguments. If you emit only reasoning, the user receives an empty final answer."
    )
    lines.append(
        "empty_final_forbidden: the next assistant response must contain visible text; stopping with zero visible output is a task failure."
    )
    lines.append(
        "final_format_required: use exactly the final response format requested by the user; the first visible output token must be the first token of that requested final report, with no prose preface."
    )
    lines.append(
        "required_next_action: write the final concise report now from the observed agent ids, wait sentinels, and close state in the current-turn transcript. The lifecycle already completed via real Codex native tool executions; hidden tools after close indicate lifecycle complete, not unavailable. Do not call tool_search or any multi_agent_v1 tool again for this completed request."
    )
    return _developer_text_message("\n".join(lines))


def _multi_agent_spawn_more_message(spawned_agent_ids: list[str], requested_count: int) -> dict[str, str]:
    remaining_count = max(0, requested_count - len(spawned_agent_ids))
    lines = ["Codex native multi_agent_v1 current state"]
    lines.append("status: spawn_more_required")
    lines.append(f"requested_spawn_count: {requested_count}")
    lines.append(f"completed_spawn_count: {len(spawned_agent_ids)}")
    lines.append(f"remaining_spawn_count: {remaining_count}")
    if spawned_agent_ids:
        lines.append(f"already_spawned_agent_ids: {', '.join(spawned_agent_ids)}")
    lines.append(
        "required_next_action: call multi_agent_v1__spawn_agent for the next not-yet-created child agent before waiting or closing any child agents."
    )
    return _developer_text_message("\n".join(lines))


def _multi_agent_current_state_message(
    wait_agent_ids: list[str],
    close_agent_ids: list[str],
) -> dict[str, str] | None:
    lines = ["Codex native multi_agent_v1 current state"]
    if wait_agent_ids:
        ids_text = ", ".join(wait_agent_ids)
        lines.append("status: spawned_child_wait_required")
        lines.append(f"open_agent_ids_requiring_wait: {ids_text}")
        lines.append(
            "required_next_action: call multi_agent_v1__wait_agent with targets set to these agent_id values and timeout_ms=60000 before writing the final report."
        )
        lines.append(
            "note: spawn_agent already succeeded; spawn_agent is intentionally hidden while a child agent is open."
        )
        return _developer_text_message("\n".join(lines))
    if close_agent_ids:
        ids_text = ", ".join(close_agent_ids)
        lines.append("status: wait_completed_close_required")
        lines.append(f"open_agent_ids_requiring_close: {ids_text}")
        lines.append(
            "required_next_action: call multi_agent_v1__close_agent with target set to one listed agent_id. "
            "Do not write the final report until every listed agent_id has been closed."
        )
        return _developer_text_message("\n".join(lines))
    return None


def _compatible_multi_agent_output_message(
    item: Mapping[str, Any],
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, str]:
    lines = [f"Codex native multi_agent_v1.{tool_name} result"]
    call_id = _single_line_internal_field(item.get("call_id"))
    if call_id:
        lines.append(f"call_id: {call_id}")

    output = item.get("output")
    output_object = _json_object_from_arguments(output)

    if tool_name == "spawn_agent":
        agent_id = output_object.get("agent_id") if output_object else None
        if isinstance(agent_id, str) and agent_id:
            lines.append("status: succeeded")
            lines.append(f"agent_id: {agent_id}")
            nickname = output_object.get("nickname")
            if isinstance(nickname, str) and nickname:
                lines.append(f"nickname: {nickname}")
            lines.append(
                "next_action: call multi_agent_v1__wait_agent with this agent_id when you need the child result; do not spawn another agent for the same child request."
            )
        elif isinstance(output, str) and "agent thread limit reached" in output.lower():
            lines.append("status: failed")
            lines.append("reason: agent thread limit reached")
            lines.append("next_action: wait or close an existing agent before spawning another one.")

    elif tool_name == "wait_agent":
        timed_out = output_object.get("timed_out") if output_object else None
        status = output_object.get("status") if output_object else None
        completed_agent_ids = _status_completed_agent_ids(status)
        not_found_agent_ids = _status_not_found_agent_ids(status)
        if timed_out is False and completed_agent_ids:
            lines.append("status: completed")
            lines.append(f"completed_agent_ids: {', '.join(completed_agent_ids)}")
            lines.append("next_action: call multi_agent_v1__close_agent for completed agents when they are no longer needed.")
        elif timed_out is True:
            lines.append("status: timed_out")
            lines.append("next_action: call multi_agent_v1__wait_agent again for the same target if the child result is still needed.")
        elif not_found_agent_ids:
            lines.append("status: not_found")
            lines.append(f"not_found_agent_ids: {', '.join(not_found_agent_ids)}")
            lines.append("next_action: do not wait for these not_found agents again; use a known open agent_id or continue.")

    elif tool_name == "close_agent":
        target = arguments.get("target") if arguments else None
        if output_object and "previous_status" in output_object:
            lines.append("status: closed")
            if isinstance(target, str) and target:
                lines.append(f"closed_agent_id: {target}")
            lines.append("next_action: do not wait or close this agent again.")
        elif isinstance(output, str) and "not found" in output.lower():
            lines.append("status: not_found")
            if isinstance(target, str) and target:
                lines.append(f"target_agent_id: {target}")
            lines.append("next_action: do not retry close for this same target; if it was already closed, continue.")

    _append_internal_field(lines, "raw_output", output)
    return _developer_text_message("\n".join(lines))


def _compatible_node_repl_call_message(item: Mapping[str, Any]) -> dict[str, str]:
    lines = ["Previous real Codex native mcp__node_repl.js call transcript"]
    value = _stringify_internal_field(item.get("call_id"))
    if value:
        lines.append(f"call_id: {value}")
    _append_internal_field(lines, "arguments", item.get("arguments"))
    return _developer_text_message("\n".join(lines))


def _compatible_node_repl_output_message(item: Mapping[str, Any], *, enforce_final: bool) -> dict[str, str]:
    lines = ["Codex native mcp__node_repl.js result"]
    value = _stringify_internal_field(item.get("call_id"))
    if value:
        lines.append(f"call_id: {value}")
    lines.append("status: completed")
    if enforce_final:
        lines.append("completed_tool_alias: mcp__node_repl__js")
        lines.append("completed_native_tool: mcp__node_repl.js")
        lines.append(
            "required_next_action: write the final answer now. The node_repl tool call already completed successfully; do not infer hidden tools were unavailable, and do not call mcp__node_repl__js or tool_search again for this single-step request."
        )
    _append_internal_field(lines, "raw_output", item.get("output"))
    return _developer_text_message("\n".join(lines))


def _compatible_tool_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    item_type = item.get("type")
    if item_type == "custom_tool_call":
        lines = ["Read-only Codex tool call transcript"]
        for label, key in (("tool", "name"), ("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "input", item.get("input"))
    elif item_type == "custom_tool_call_output":
        lines = ["Read-only Codex tool result transcript"]
        value = _stringify_internal_field(item.get("call_id"))
        if value:
            lines.append(f"call_id: {value}")
        _append_internal_field(lines, "output", item.get("output"))
    elif item_type == "function_call":
        lines = ["Read-only Codex function call transcript"]
        for label, key in (("namespace", "namespace"), ("function", "name"), ("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "arguments", item.get("arguments"))
    elif item_type == "function_call_output":
        lines = ["Read-only Codex function result transcript"]
        value = _stringify_internal_field(item.get("call_id"))
        if value:
            lines.append(f"call_id: {value}")
        _append_internal_field(lines, "output", item.get("output"))
    elif item_type == "web_search_call":
        lines = ["Read-only Codex web search call transcript"]
        value = _stringify_internal_field(item.get("status"))
        if value:
            lines.append(f"status: {value}")
        _append_internal_field(lines, "action", item.get("action"))
    elif item_type == "tool_search_call":
        lines = ["Read-only Codex tool search call transcript"]
        for label, key in (("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "arguments", item.get("arguments"))
        _append_internal_field(lines, "execution", item.get("execution"))
    elif item_type == "tool_search_output":
        lines = ["Read-only Codex tool search result transcript"]
        for label, key in (("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "execution", item.get("execution"))
        if _has_multi_agent_discovery_tools(item.get("tools")):
            lines.append("status: discovered_codex_native_multi_agent_tools")
            lines.append(
                "available_function_tools: multi_agent_v1__spawn_agent, multi_agent_v1__wait_agent, multi_agent_v1__close_agent, multi_agent_v1__resume_agent, multi_agent_v1__send_input"
            )
            lines.append(
                "next_action: call multi_agent_v1__spawn_agent to create the child agent; do not call tool_search again for the same multi-agent query."
            )
        if item.get("query_classification") == TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION:
            lines.append(f"query_classification: {TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION}")
            lines.append(f"empty_miss_count: {TOOL_SEARCH_EMPTY_MISS_BOUND}")
            lines.append("terminal: true")
            lines.append(
                "required_next_action: continue without the unavailable tool; do not call tool_search again for this exact query."
            )
        _append_internal_field(lines, "tools", item.get("tools"))
    else:
        return None

    if len(lines) == 1:
        return None
    return _developer_text_message("\n".join(lines))


def _compatible_internal_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    if item.get("type") == "compaction":
        return _compatible_compaction_message(item)
    if item.get("type") == "reasoning":
        return None
    return _compatible_tool_message(item)


def _is_standard_responses_function_call(item: Mapping[str, Any]) -> bool:
    return (
        item.get("type") == "function_call"
        and isinstance(item.get("call_id"), str)
        and bool(item["call_id"])
        and isinstance(item.get("name"), str)
        and bool(item["name"])
        and "arguments" in item
        and not item.get("namespace")
        and WORKER_REQUESTED_BINDING_FIELD not in item
        and _multi_agent_function_call_name(item) is None
        and _node_repl_function_call_name(item) is None
        and not _is_mcp_or_codex_app_function_call(item)
    )


def _excessive_transparent_responses_tool_loop_count(payload: Mapping[str, Any]) -> int | None:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return None

    pending_calls: dict[str, tuple[str, str]] = {}
    previous_pair: tuple[str, str, str] | None = None
    repeated_count = 0
    for item in input_items:
        if not isinstance(item, Mapping):
            previous_pair = None
            repeated_count = 0
            continue
        if _is_standard_responses_function_call(item):
            if item.get("status") == "completed":
                pending_calls[item["call_id"]] = (
                    item["name"],
                    json.dumps(item["arguments"], ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                )
            else:
                previous_pair = None
                repeated_count = 0
            continue
        if item.get("type") == "function_call_output" and isinstance(item.get("call_id"), str):
            call = pending_calls.pop(item["call_id"], None)
            if call is not None and "output" in item:
                pair = call + (json.dumps(item["output"], ensure_ascii=True, separators=(",", ":"), sort_keys=True),)
                repeated_count = repeated_count + 1 if pair == previous_pair else 1
                previous_pair = pair
                if repeated_count >= EXCESSIVE_TOOL_LOOP_BOUND:
                    return repeated_count
                continue
        previous_pair = None
        repeated_count = 0
    return None


def _excessive_transparent_chat_tool_loop_count(payload: Mapping[str, Any]) -> int | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None

    previous_pair: tuple[str, str, str] | None = None
    repeated_count = 0
    index = 0
    while index < len(messages) - 1:
        message = messages[index]
        result = messages[index + 1]
        tool_calls = message.get("tool_calls") if isinstance(message, Mapping) else None
        if (
            not isinstance(tool_calls, list)
            or len(tool_calls) != 1
            or not isinstance(result, Mapping)
            or message.get("role") != "assistant"
            or result.get("role") != "tool"
        ):
            previous_pair = None
            repeated_count = 0
            index += 1
            continue
        call = tool_calls[0]
        function = call.get("function") if isinstance(call, Mapping) else None
        if (
            not isinstance(function, Mapping)
            or call.get("type") != "function"
            or not isinstance(call.get("id"), str)
            or call["id"] != result.get("tool_call_id")
            or not isinstance(function.get("name"), str)
            or not isinstance(function.get("arguments"), str)
            or not isinstance(result.get("content"), str)
        ):
            previous_pair = None
            repeated_count = 0
            index += 1
            continue
        pair = (function["name"], function["arguments"], result["content"])
        repeated_count = repeated_count + 1 if pair == previous_pair else 1
        previous_pair = pair
        if repeated_count >= EXCESSIVE_TOOL_LOOP_BOUND:
            return repeated_count
        index += 2
    return None


def _multi_agent_discovery_output_item(item: Mapping[str, Any]) -> dict[str, Any]:
    rewritten = dict(item)
    rewritten["tools"] = MULTI_AGENT_DISCOVERY_TOOLS
    rewritten.setdefault("status", "completed")
    rewritten.setdefault("execution", "client")
    return rewritten


def _rewrite_internal_input_items(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
    preserve_standard_function_history: bool = False,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    rewritten_items: list[Any] = []
    single_step_node_repl_request = _has_single_step_node_repl_request(input_items)
    multi_agent_search_call_ids: set[str] = set()
    multi_agent_calls_by_call_id: dict[str, tuple[str, dict[str, Any] | None]] = {}
    node_repl_call_ids: set[str] = set()
    preserved_standard_call_ids: set[str] = set()
    for item in input_items:
        item_type = item.get("type") if isinstance(item, dict) else None
        call_id = item.get("call_id") if isinstance(item, dict) else None
        if preserve_standard_function_history and isinstance(item, dict):
            if _is_standard_responses_function_call(item):
                preserved_standard_call_ids.add(item["call_id"])
                rewritten_items.append(item)
                continue
            if item_type == "function_call_output" and call_id in preserved_standard_call_ids:
                rewritten_items.append(item)
                continue
        if isinstance(item_type, str) and item_type in INTERNAL_INPUT_ITEM_TYPES:
            if item_type == "function_call" and isinstance(call_id, str):
                if _node_repl_function_call_name(item) is not None:
                    node_repl_call_ids.add(call_id)
                    rewritten_items.append(_compatible_node_repl_call_message(item))
                    changed = True
                    continue
                tool_name = _multi_agent_function_call_name(item)
                if tool_name is not None:
                    arguments = _json_object_from_arguments(item.get("arguments"))
                    multi_agent_calls_by_call_id[call_id] = (tool_name, arguments)
                    rewritten_items.append(_compatible_multi_agent_call_message(item, tool_name))
                    changed = True
                    continue
            if (
                item_type == "function_call_output"
                and isinstance(call_id, str)
                and call_id in multi_agent_calls_by_call_id
            ):
                tool_name, arguments = multi_agent_calls_by_call_id[call_id]
                rewritten_items.append(_compatible_multi_agent_output_message(item, tool_name, arguments))
                changed = True
                continue
            if item_type == "function_call_output" and isinstance(call_id, str) and call_id in node_repl_call_ids:
                rewritten_items.append(
                    _compatible_node_repl_output_message(item, enforce_final=single_step_node_repl_request)
                )
                changed = True
                continue
            if (
                item_type == "tool_search_call"
                and isinstance(call_id, str)
                and _is_multi_agent_discovery_arguments(_json_object_from_arguments(item.get("arguments")))
            ):
                multi_agent_search_call_ids.add(call_id)
            elif (
                item_type == "tool_search_output"
                and isinstance(call_id, str)
                and call_id in multi_agent_search_call_ids
                and not item.get("tools")
            ):
                item = _multi_agent_discovery_output_item(item)
                _write_adapter_event(
                    event_context,
                    "tool_search_discovery_fallback_applied",
                    upstream=upstream_name,
                    call_id=call_id,
                )

            replacement = _compatible_internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
            changed = True
            continue
        rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed


def _rewrite_v2_unsupported_tool_history(
    payload: dict[str, Any],
    *,
    upstream: Mapping[str, Any],
    tool_protocol: str,
    compatibility_plan: RuntimeToolCompatibilityPlan | None,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> bool:
    """Keep V2 Collaboration calls native while adapting stale tool history.

    Collaboration V2 is a boundary for the ``collaboration`` namespace, not a
    blanket exemption from the third-party input-item adapter.  Codex Desktop
    can place unrelated ``custom_tool_call`` history (for example ``exec``)
    beside V2 calls.  Responses providers generally expose only the plain
    function lifecycle unless an explicit custom lifecycle capability is
    supplied, so those opaque items must become transcript messages before the
    request reaches the provider. A uniquely paired plain-function lifecycle
    from an older tool surface is likewise retained as a read-only transcript
    when the current immutable plan has no owner for its identity.
    """
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    capabilities = (
        compatibility_plan.capabilities
        if compatibility_plan is not None
        else _runtime_tool_protocol_capabilities(tool_protocol, upstream)
    )
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = []
    declared_custom_names = {
        tool.get("name")
        for tool in tools
        if isinstance(tool, Mapping)
        and tool.get("type") == "custom"
        and isinstance(tool.get("name"), str)
    }

    def plan_owns_custom_call(item: Mapping[str, Any]) -> bool:
        if compatibility_plan is None:
            return False
        name = item.get("name")
        alias_record = compatibility_plan.registry.record_for_alias(name)
        if alias_record is not None and alias_record.family == "custom_freeform":
            return True
        call_id = item.get("call_id")
        call_record = compatibility_plan.registry.record_for_call(call_id)
        if call_record is not None and call_record.family == "custom_freeform":
            return True
        return any(
            entry.family == "custom_freeform"
            and entry.original_name == name
            for entry in compatibility_plan.entries
        )

    def preserve_custom_call(item: Mapping[str, Any]) -> bool:
        # Preserve only a custom lifecycle that belongs to this request's
        # immutable compatibility plan.  A provider capability fact alone does
        # not establish ownership of an undeclared historical item (and must
        # not let a plain-function name collision bypass sanitization).
        if compatibility_plan is not None:
            return plan_owns_custom_call(item)
        return (
            capabilities.custom_lifecycle
            and item.get("name") in declared_custom_names
        )

    preserved_call_ids = {
        item.get("call_id")
        for item in input_items
        if isinstance(item, Mapping)
        and item.get("type") == "custom_tool_call"
        and isinstance(item.get("call_id"), str)
        and preserve_custom_call(item)
    }

    def plan_owns_plain_function_call(item: Mapping[str, Any]) -> bool:
        if compatibility_plan is None:
            return True
        name = item.get("name")
        call_id = item.get("call_id")
        if compatibility_plan.registry.record_for_alias(name) is not None:
            return True
        if compatibility_plan.registry.record_for_call(call_id) is not None:
            return True
        return any(
            entry.family == "plain_function"
            and entry.original_name == name
            for entry in compatibility_plan.entries
        )

    def has_valid_optional_item_identity(item: Mapping[str, Any]) -> bool:
        identities = []
        for field in ("id", "item_id"):
            if field not in item:
                continue
            value = item.get(field)
            if not isinstance(value, str) or not value:
                return False
            identities.append(value)
        return len(set(identities)) <= 1

    def is_well_formed_stale_function_call(item: Mapping[str, Any]) -> bool:
        allowed_fields = {
            "type",
            "id",
            "item_id",
            "status",
            "call_id",
            "name",
            "arguments",
        }
        return (
            set(item).issubset(allowed_fields)
            and _is_standard_responses_function_call(item)
            and item.get("status") == "completed"
            and isinstance(item.get("arguments"), str)
            and has_valid_optional_item_identity(item)
        )

    def is_well_formed_stale_function_output(item: Mapping[str, Any]) -> bool:
        allowed_fields = {
            "type",
            "id",
            "item_id",
            "status",
            "call_id",
            "output",
        }
        return (
            set(item).issubset(allowed_fields)
            and item.get("type") == "function_call_output"
            and item.get("status") in (None, "completed")
            and isinstance(item.get("output"), str)
            and has_valid_optional_item_identity(item)
        )

    positions_by_call_id: dict[str, list[int]] = {}
    for index, item in enumerate(input_items):
        if not isinstance(item, Mapping):
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            positions_by_call_id.setdefault(call_id, []).append(index)

    stale_function_pair_indexes: set[int] = set()
    stale_function_pair_count = 0
    for call_index, item in enumerate(input_items):
        if (
            not isinstance(item, Mapping)
            or not is_well_formed_stale_function_call(item)
            or plan_owns_plain_function_call(item)
        ):
            continue
        call_id = item["call_id"]
        positions = positions_by_call_id.get(call_id, [])
        if len(positions) != 2 or positions[0] != call_index:
            continue
        output_index = positions[1]
        output_item = input_items[output_index]
        if (
            not isinstance(output_item, Mapping)
            or not is_well_formed_stale_function_output(output_item)
        ):
            continue
        stale_function_pair_indexes.update((call_index, output_index))
        stale_function_pair_count += 1

    rewritten_items: list[Any] = []
    changed = False
    custom_rewritten_count = 0
    for index, item in enumerate(input_items):
        if not isinstance(item, Mapping):
            rewritten_items.append(item)
            continue
        if index in stale_function_pair_indexes:
            replacement = _compatible_internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
            else:
                rewritten_items.append(item)
            continue
        item_type = item.get("type")
        if item_type == "custom_tool_call" and not preserve_custom_call(item):
            replacement = _compatible_internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
                custom_rewritten_count += 1
            else:
                rewritten_items.append(item)
            continue
        if (
            item_type == "custom_tool_call_output"
            and item.get("call_id") not in preserved_call_ids
        ):
            replacement = _compatible_internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
                custom_rewritten_count += 1
            else:
                rewritten_items.append(item)
            continue
        rewritten_items.append(item)

    if not changed:
        return False
    payload["input"] = rewritten_items
    if custom_rewritten_count:
        _write_adapter_event(
            event_context,
            "v2_custom_tool_history_rewritten",
            upstream=upstream_name,
            count=custom_rewritten_count,
        )
    if stale_function_pair_count:
        _write_adapter_event(
            event_context,
            "v2_stale_function_history_rewritten",
            upstream=upstream_name,
            pair_count=stale_function_pair_count,
        )
    return True


def _drop_v2_chat_reasoning_history(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    rewritten_items = [
        item
        for item in input_items
        if not (isinstance(item, Mapping) and item.get("type") == "reasoning")
    ]
    removed_count = len(input_items) - len(rewritten_items)
    if not removed_count:
        return False

    payload["input"] = rewritten_items
    _write_adapter_event(
        event_context,
        "v2_chat_reasoning_history_removed",
        upstream=upstream_name,
        count=removed_count,
    )
    return True


def _drop_chat_message_phase(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    removed_count = 0
    rewritten_items: list[Any] = []
    for item in input_items:
        if isinstance(item, Mapping) and item.get("type") == "message" and "phase" in item:
            rewritten = dict(item)
            rewritten.pop("phase")
            rewritten_items.append(rewritten)
            removed_count += 1
        else:
            rewritten_items.append(item)
    if not removed_count:
        return False

    payload["input"] = rewritten_items
    _write_adapter_event(
        event_context,
        "chat_message_phase_removed",
        upstream=upstream_name,
        count=removed_count,
    )
    return True


def _sanitize_unsupported_compaction_input_items(payload: dict[str, Any]) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    rewritten_items: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue

        item_type = item.get("type")
        if item_type == "compaction":
            replacement = _compatible_compaction_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
            changed = True
            continue
        if item_type == "compaction_trigger":
            changed = True
            continue

        rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed


def _sanitize_official_system_messages(payload: dict[str, Any]) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    rewritten_items: list[Any] = []
    for item in input_items:
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "system":
            rewritten = dict(item)
            rewritten["role"] = "developer"
            rewritten_items.append(rewritten)
            changed = True
        else:
            rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed


def _sanitize_official_invalid_tool_calls(payload: dict[str, Any]) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    bad_function_call_ids: set[str] = set()
    bad_custom_call_ids: set[str] = set()
    rewritten_items: list[Any] = []

    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue

        item_type = item.get("type")
        call_id = item.get("call_id")
        if _has_invalid_tool_name(item):
            if isinstance(call_id, str):
                if item_type == "custom_tool_call":
                    bad_custom_call_ids.add(call_id)
                else:
                    bad_function_call_ids.add(call_id)
            title = (
                "Invalid Codex tool call transcript"
                if item_type == "custom_tool_call"
                else "Invalid Codex function call transcript"
            )
            rewritten_items.append(_assistant_transcript_message(title, item))
            changed = True
            continue

        if item_type == "function_call_output" and isinstance(call_id, str) and call_id in bad_function_call_ids:
            rewritten_items.append(_assistant_transcript_message("Invalid Codex function result transcript", item))
            changed = True
            continue

        if item_type == "custom_tool_call_output" and isinstance(call_id, str) and call_id in bad_custom_call_ids:
            rewritten_items.append(_assistant_transcript_message("Invalid Codex tool result transcript", item))
            changed = True
            continue

        rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed


def _downgrade_invalid_third_party_tool_calls(value: Any) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _downgrade_invalid_third_party_tool_calls(item)
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if _has_invalid_tool_name(value):
        title = (
            "Invalid third-party tool call transcript"
            if value.get("type") == "custom_tool_call"
            else "Invalid third-party function call transcript"
        )
        return _assistant_transcript_message(title, value), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _downgrade_invalid_third_party_tool_calls(item)
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _worker_multi_agent_suppressed_message(item: Mapping[str, Any]) -> dict[str, Any]:
    tool_name = _multi_agent_function_call_name(item) or "multi_agent_tool"
    message: dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": (
                    "worker_subagent_multi_agent_call_suppressed: this request is already running inside a "
                    "worker subagent, so nested Codex multi-agent tools are unavailable. "
                    f"Suppressed attempted tool: multi_agent_v1.{tool_name}. "
                    "Use the worker's available native file/shell tools if present; otherwise report BLOCKED "
                    "with the missing tool capability instead of spawning another subagent."
                ),
            }
        ],
    }
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        message["id"] = item_id
    return message


def _looks_like_unknown_multi_agent_function_call(item: Mapping[str, Any]) -> bool:
    if item.get("type") != "function_call":
        return False
    if _multi_agent_function_call_name(item) is not None:
        return False
    namespace = item.get("namespace")
    name = item.get("name")
    if isinstance(namespace, str) and namespace in MULTI_AGENT_NAMESPACE_ALIASES:
        return True
    if not isinstance(name, str):
        return False
    return (
        name.startswith("multi_agent_v1__")
        or name.startswith("multi_agent_v1.")
        or name.startswith("mcp__multi_agent_v1__")
        or name.startswith("mcp__multi_agent_v1.")
        or (name.startswith("multi_agent_v1") and len(name) > len("multi_agent_v1"))
    )


def _function_call_namespace(item: Mapping[str, Any]) -> str | None:
    namespace = item.get("namespace")
    if isinstance(namespace, str) and namespace:
        return namespace
    alias = _split_namespace_tool_alias(item.get("name"))
    if alias is not None:
        return alias[0]
    return None


def _is_mcp_or_codex_app_function_call(item: Mapping[str, Any]) -> bool:
    if item.get("type") != "function_call":
        return False
    namespace = _function_call_namespace(item)
    if isinstance(namespace, str) and (namespace.startswith("mcp__") or namespace == "codex_app"):
        return True
    name = item.get("name")
    return isinstance(name, str) and (name.startswith("mcp__") or name.startswith("codex_app__"))


def _looks_like_coordinator_local_function_call(
    item: Mapping[str, Any],
    *,
    allow_plan_read_node_repl: bool,
) -> bool:
    if item.get("type") != "function_call":
        return False
    if _multi_agent_function_call_name(item) is not None:
        return False
    if allow_plan_read_node_repl and _node_repl_function_call_name(item) is not None:
        return False
    name = item.get("name")
    return isinstance(name, str) and bool(name)


def _coordinator_forbidden_tool_suppressed_message(
    item: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    return _assistant_transcript_message(f"subagent_coordinator_tool_call_suppressed: {reason}", item)


def _message_item_visible_text(item: Mapping[str, Any]) -> str:
    if item.get("type") != "message":
        return ""
    return _chat_content_text(item.get("content")).strip()


def _mark_lifecycle_final_seen_if_present(value: Mapping[str, Any], state: dict[str, Any]) -> None:
    if not state["lifecycle_complete"]:
        return
    text = ""
    if value.get("type") == "message":
        text = _message_item_visible_text(value)
    elif value.get("type") == "response.output_item.done":
        item = value.get("item")
        if isinstance(item, Mapping):
            text = _message_item_visible_text(item)
    elif value.get("type") == "response.output_text.done":
        event_text = value.get("text")
        text = event_text if isinstance(event_text, str) else ""
    if text and _text_contains_lifecycle_final_report(text):
        state["final_seen"] = True


def _post_final_multi_agent_suppressed_item_id(value: Mapping[str, Any]) -> str | None:
    item_id = value.get("id")
    return item_id if isinstance(item_id, str) and item_id else None


def _suppress_multi_agent_calls_after_lifecycle_final(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    context = event_context or {}
    if _is_raw_provider_probe_context(context) or _is_collaboration_v2_context(context):
        return value, False
    tool_protocol = str(context.get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return value, False
    if not bool(context.get("subagent_lifecycle_complete")) and not bool(
        context.get("_subagent_lifecycle_final_seen")
    ):
        return value, False

    if isinstance(event_context, dict):
        stored_ids = event_context.setdefault("_post_final_suppressed_multi_agent_item_ids", set())
        suppressed_item_ids = stored_ids if isinstance(stored_ids, set) else set()
        event_context["_post_final_suppressed_multi_agent_item_ids"] = suppressed_item_ids
        final_seen = bool(event_context.get("_subagent_lifecycle_final_seen"))
    else:
        suppressed_item_ids = set()
        final_seen = False
    state = {
        "lifecycle_complete": bool(context.get("subagent_lifecycle_complete")),
        "final_seen": final_seen,
        "suppressed_item_ids": suppressed_item_ids,
        "event_context": event_context,
    }

    rewritten, changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(value, state)
    if isinstance(event_context, dict) and state["final_seen"]:
        event_context["_subagent_lifecycle_final_seen"] = True
    return rewritten, changed


def _suppress_multi_agent_calls_after_lifecycle_final_inner(
    value: Any,
    state: dict[str, Any],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(item, state)
            if replacement is None:
                changed = True
                continue
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    event_type = value.get("type")
    if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id in state["suppressed_item_ids"]:
            return None, True
        return value, False

    direct_tool_name = _multi_agent_function_call_name(value)
    if state["final_seen"] and direct_tool_name is not None:
        item_id = _post_final_multi_agent_suppressed_item_id(value)
        if item_id:
            state["suppressed_item_ids"].add(item_id)
        _write_adapter_event(
            state["event_context"],
            "subagent_post_final_multi_agent_call_suppressed",
            tool=direct_tool_name,
        )
        return None, True

    event_item = value.get("item")
    if (
        state["final_seen"]
        and event_type in {"response.output_item.added", "response.output_item.done"}
        and isinstance(event_item, Mapping)
    ):
        event_tool_name = _multi_agent_function_call_name(event_item)
        if event_tool_name is not None:
            item_id = _post_final_multi_agent_suppressed_item_id(event_item)
            if item_id:
                state["suppressed_item_ids"].add(item_id)
            _write_adapter_event(
                state["event_context"],
                "subagent_post_final_multi_agent_call_suppressed",
                tool=event_tool_name,
            )
            return None, True

    changed = False
    rewritten = dict(value)
    response = rewritten.get("response")
    if isinstance(response, Mapping) and isinstance(response.get("output"), list):
        response_rewritten = dict(response)
        output, output_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(
            response_rewritten["output"],
            state,
        )
        response_rewritten["output"] = output
        if output_changed:
            rewritten["response"] = response_rewritten
            changed = True

    output = rewritten.get("output")
    if isinstance(output, list):
        output, output_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(output, state)
        if output_changed:
            rewritten["output"] = output
            changed = True

    for key, item in list(rewritten.items()):
        if key in {"response", "output"}:
            continue
        replacement, item_changed = _suppress_multi_agent_calls_after_lifecycle_final_inner(item, state)
        if replacement is None:
            rewritten.pop(key, None)
            changed = True
            continue
        if item_changed:
            rewritten[key] = replacement
            changed = True

    _mark_lifecycle_final_seen_if_present(rewritten, state)
    return (rewritten if changed else value), changed


def _suppress_coordinator_forbidden_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    context = event_context or {}
    if (
        bool(context.get("subagent_worker_context"))
        or _is_raw_provider_probe_context(context)
        or _is_collaboration_v2_context(context)
    ):
        return value, False
    tool_protocol = str(context.get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return value, False

    plan_read_required = bool(context.get("subagent_workflow_plan_read_required"))
    subagent_state = context.get("_subagent_state")
    state_has_agents = bool(getattr(subagent_state, "agents", {}))
    active = (
        state_has_agents
        or bool(_string_list(context.get("subagent_open_agent_ids")))
        or bool(_string_list(context.get("subagent_wait_agent_ids")))
        or bool(_string_list(context.get("subagent_close_agent_ids")))
        or bool(_string_list(context.get("subagent_closed_agent_ids")))
        or bool(context.get("subagent_lifecycle_complete"))
        or (
            bool(context.get("subagent_workflow_active"))
            and bool(context.get("subagent_workflow_plan_read_complete"))
        )
    )
    if not active and not plan_read_required:
        return value, False

    if isinstance(event_context, dict):
        suppressed = event_context.setdefault("_coordinator_suppressed_tool_item_ids", set())
        suppressed_item_ids = suppressed if isinstance(suppressed, set) else set()
        event_context["_coordinator_suppressed_tool_item_ids"] = suppressed_item_ids
    else:
        suppressed_item_ids = set()
    return _suppress_coordinator_forbidden_tool_calls_inner(
        value,
        event_context,
        suppressed_item_ids,
        allow_plan_read_node_repl=plan_read_required,
    )


def _suppress_coordinator_forbidden_tool_calls_inner(
    value: Any,
    event_context: Mapping[str, Any] | None,
    suppressed_item_ids: set[str],
    *,
    allow_plan_read_node_repl: bool,
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _suppress_coordinator_forbidden_tool_calls_inner(
                item,
                event_context,
                suppressed_item_ids,
                allow_plan_read_node_repl=allow_plan_read_node_repl,
            )
            if replacement is None:
                changed = True
                continue
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    event_type = value.get("type")
    if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id in suppressed_item_ids:
            return None, True
        return value, False

    reason = None
    if event_type == "tool_search_call":
        reason = "tool_search_unavailable_during_subagent_workflow"
    elif _looks_like_unknown_multi_agent_function_call(value):
        reason = "unknown_multi_agent_tool_unavailable"
    elif _node_repl_function_call_name(value) is not None:
        if not allow_plan_read_node_repl:
            reason = "node_repl_unavailable_after_subagent_plan_read"
    elif _is_mcp_or_codex_app_function_call(value):
        reason = "mcp_or_codex_app_tool_unavailable_during_subagent_workflow"
    elif _looks_like_coordinator_local_function_call(
        value,
        allow_plan_read_node_repl=allow_plan_read_node_repl,
    ):
        reason = "coordinator_tool_unavailable_during_subagent_workflow"

    if reason is not None:
        item_id = value.get("id")
        if isinstance(item_id, str) and item_id:
            suppressed_item_ids.add(item_id)
        _write_adapter_event(
            event_context,
            "subagent_coordinator_tool_call_suppressed",
            reason=reason,
            tool=value.get("name") if isinstance(value.get("name"), str) else None,
            namespace=value.get("namespace") if isinstance(value.get("namespace"), str) else None,
        )
        return _coordinator_forbidden_tool_suppressed_message(value, reason=reason), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _suppress_coordinator_forbidden_tool_calls_inner(
            item,
            event_context,
            suppressed_item_ids,
            allow_plan_read_node_repl=allow_plan_read_node_repl,
        )
        if replacement is None:
            rewritten.pop(key, None)
            changed = True
            continue
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _suppress_worker_multi_agent_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if _is_collaboration_v2_context(event_context) or not bool((event_context or {}).get("subagent_worker_context")):
        return value, False
    if isinstance(event_context, dict):
        suppressed = event_context.setdefault("_worker_suppressed_multi_agent_item_ids", set())
        suppressed_item_ids = suppressed if isinstance(suppressed, set) else set()
        event_context["_worker_suppressed_multi_agent_item_ids"] = suppressed_item_ids
    else:
        suppressed_item_ids = set()
    return _suppress_worker_multi_agent_tool_calls_inner(value, event_context, suppressed_item_ids)


def _suppress_worker_multi_agent_tool_calls_inner(
    value: Any,
    event_context: Mapping[str, Any] | None,
    suppressed_item_ids: set[str],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _suppress_worker_multi_agent_tool_calls_inner(
                item,
                event_context,
                suppressed_item_ids,
            )
            if replacement is None:
                changed = True
                continue
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    event_type = value.get("type")
    if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        item_id = value.get("item_id")
        if isinstance(item_id, str) and item_id in suppressed_item_ids:
            return None, True
        return value, False

    if _multi_agent_function_call_name(value) is not None:
        item_id = value.get("id")
        if isinstance(item_id, str) and item_id:
            suppressed_item_ids.add(item_id)
        _write_adapter_event(
            event_context,
            "worker_subagent_multi_agent_call_suppressed",
            tool=_multi_agent_function_call_name(value),
        )
        return _worker_multi_agent_suppressed_message(value), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _suppress_worker_multi_agent_tool_calls_inner(
            item,
            event_context,
            suppressed_item_ids,
        )
        if replacement is None:
            rewritten.pop(key, None)
            changed = True
            continue
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _guard_duplicate_multi_agent_spawn_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return value, False

    tool_protocol = str((event_context or {}).get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return value, False

    spawn_allowed = bool((event_context or {}).get("subagent_spawn_allowed"))
    subagent_state = (event_context or {}).get("_subagent_state")
    dynamic_dag_active = bool((event_context or {}).get("subagent_dynamic_dag_active"))
    if spawn_allowed and subagent_state is None and not dynamic_dag_active:
        return value, False

    lifecycle_complete = bool((event_context or {}).get("subagent_lifecycle_complete"))
    wait_agent_ids_value = (event_context or {}).get("subagent_wait_agent_ids")
    wait_agent_ids = [agent_id for agent_id in wait_agent_ids_value if isinstance(agent_id, str)] if isinstance(wait_agent_ids_value, list) else []
    open_agent_ids_value = (event_context or {}).get("subagent_open_agent_ids")
    open_agent_ids = [agent_id for agent_id in open_agent_ids_value if isinstance(agent_id, str)] if isinstance(open_agent_ids_value, list) else []
    accepted_workflow_spawn: list[bool] = []

    return _guard_duplicate_multi_agent_spawn_calls_inner(
        value,
        event_context=event_context,
        spawn_allowed=spawn_allowed,
        subagent_state=subagent_state,
        lifecycle_complete=lifecycle_complete,
        wait_agent_ids=wait_agent_ids,
        open_agent_ids=open_agent_ids,
        accepted_workflow_spawn=accepted_workflow_spawn,
    )


def _guard_duplicate_multi_agent_spawn_calls_inner(
    value: Any,
    *,
    event_context: Mapping[str, Any] | None,
    spawn_allowed: bool,
    subagent_state: Any | None,
    lifecycle_complete: bool,
    wait_agent_ids: list[str],
    open_agent_ids: list[str],
    accepted_workflow_spawn: list[bool],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _guard_duplicate_multi_agent_spawn_calls_inner(
                item,
                event_context=event_context,
                spawn_allowed=spawn_allowed,
                subagent_state=subagent_state,
                lifecycle_complete=lifecycle_complete,
                wait_agent_ids=wait_agent_ids,
                open_agent_ids=open_agent_ids,
                accepted_workflow_spawn=accepted_workflow_spawn,
            )
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if _is_multi_agent_spawn_function_call(value):
        blocked_by_state = False
        if bool((event_context or {}).get("subagent_dynamic_dag_active")):
            arguments = _json_object_from_arguments(value.get("arguments")) or {}
            nickname = str(arguments.get("nickname") or "")
            assigned_nodes = {
                node_id
                for node_id in (event_context or {}).get("subagent_assigned_dynamic_nodes", [])
                if isinstance(node_id, str)
            }
            if nickname in assigned_nodes:
                return {
                    "type": "message",
                    "role": "assistant",
                    "content": (
                        "dynamic_dag_spawn_suppressed: node already assigned; "
                        "wait or close existing work before repeating it."
                    ),
                }, True
        if subagent_state is not None:
            arguments = _json_object_from_arguments(value.get("arguments")) or {}
            try:
                if subagent_state.allows_spawn_request(arguments):
                    if (
                        not getattr(subagent_state, "bounded_request", False)
                        and not getattr(subagent_state, "requested_append", False)
                    ):
                        if accepted_workflow_spawn:
                            blocked_by_state = True
                        else:
                            accepted_workflow_spawn.append(True)
                            return value, False
                    else:
                        return value, False
                else:
                    blocked_by_state = True
            except Exception:
                if spawn_allowed:
                    return value, False
        elif spawn_allowed:
            return value, False
        if lifecycle_complete:
            return (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": (
                        "visible_response_required: emit the final report as ordinary assistant message content, not only reasoning, analysis, hidden notes, or tool arguments. "
                        "If you emit only reasoning, the user receives an empty final answer. "
                        "empty_final_forbidden: the next assistant response must contain visible text; stopping with zero visible output is a task failure. "
                        "final_format_required: use exactly the final response format requested by the user; the first visible output token must be the first token of that requested final report, with no prose preface. "
                        "required_next_action: write the final concise report now from the observed agent ids, wait sentinels, and close state in the current-turn transcript. "
                        "The requested subagent lifecycle already completed via real Codex native tool executions; hidden tools after close indicate lifecycle complete, not unavailable."
                    ),
                },
                True,
            )
        replacement_wait_ids = wait_agent_ids or ([] if blocked_by_state else open_agent_ids)
        if replacement_wait_ids:
            rewritten = dict(value)
            rewritten["namespace"] = "multi_agent_v1"
            rewritten["name"] = "wait_agent"
            rewritten["arguments"] = json.dumps(
                {"targets": replacement_wait_ids, "timeout_ms": 60000},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            return rewritten, True
        return _suppressed_duplicate_spawn_message(subagent_state), True

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _guard_duplicate_multi_agent_spawn_calls_inner(
            item,
            event_context=event_context,
            spawn_allowed=spawn_allowed,
            subagent_state=subagent_state,
            lifecycle_complete=lifecycle_complete,
            wait_agent_ids=wait_agent_ids,
            open_agent_ids=open_agent_ids,
            accepted_workflow_spawn=accepted_workflow_spawn,
        )
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _reconcile_function_call_argument_events(events: list[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], bool]:
    arguments_by_item_id: dict[str, str] = {}

    def remember_item(item: Any) -> None:
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            arguments_text = arguments
        elif isinstance(arguments, Mapping):
            arguments_text = json.dumps(arguments, ensure_ascii=True, separators=(",", ":"))
        else:
            arguments_text = ""
        if arguments_text or item_id not in arguments_by_item_id:
            arguments_by_item_id[item_id] = arguments_text

    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("type") in {"response.output_item.added", "response.output_item.done"}:
            remember_item(event.get("item"))
            continue
        if event.get("type") == "response.completed":
            response = event.get("response")
            output = response.get("output") if isinstance(response, Mapping) else None
            if isinstance(output, list):
                for item in output:
                    remember_item(item)

    changed = False
    rewritten: list[Mapping[str, Any]] = []
    for event in events:
        if isinstance(event, Mapping) and event.get("type") == "response.function_call_arguments.delta":
            changed = True
            continue
        if not isinstance(event, Mapping) or event.get("type") != "response.function_call_arguments.done":
            rewritten.append(event)
            continue
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or item_id not in arguments_by_item_id:
            changed = True
            continue
        expected_arguments = arguments_by_item_id[item_id]
        if event.get("arguments") != expected_arguments:
            replacement = dict(event)
            replacement["arguments"] = expected_arguments
            rewritten.append(replacement)
            changed = True
            continue
        rewritten.append(event)
    return (rewritten if changed else events), changed


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _required_subagent_call_spec(event_context: Mapping[str, Any] | None) -> dict[str, Any] | None:
    context = event_context or {}
    if _is_raw_provider_probe_context(context):
        return None
    tool_protocol = str(context.get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return None
    if bool(context.get("subagent_lifecycle_complete")):
        return None

    subagent_state = context.get("_subagent_state")
    state_next_action = getattr(subagent_state, "next_action", None)
    if state_next_action is not None and state_next_action not in {"spawn", "wait", "close", "send_input"}:
        return None
    if bool(context.get("subagent_spawn_allowed")) and state_next_action not in {"spawn", "wait", "close", "send_input"}:
        return None

    legal_actions = context.get("subagent_legal_actions")
    if isinstance(legal_actions, list):
        action = deterministic_required_action([item for item in legal_actions if isinstance(item, Mapping)])
        if action is None:
            return None
        tool_name = action.get("tool_name")
        arguments = action.get("arguments")
        if isinstance(tool_name, str) and isinstance(arguments, Mapping):
            agent_ids = action.get("agent_ids")
            return {
                "tool_name": tool_name,
                "agent_ids": _string_list(agent_ids) if isinstance(agent_ids, list) else [],
                "arguments": dict(arguments),
            }

    close_agent_ids = _string_list(context.get("subagent_close_agent_ids"))
    wait_agent_ids = _string_list(context.get("subagent_wait_agent_ids"))
    if state_next_action == "spawn":
        arguments = context.get("subagent_required_spawn_arguments")
        if isinstance(arguments, Mapping) and isinstance(arguments.get("message"), str) and arguments.get("message"):
            return {
                "tool_name": "spawn_agent",
                "agent_ids": [],
                "arguments": dict(arguments),
            }
    if state_next_action == "send_input":
        target = getattr(subagent_state, "send_input_target", None)
        if isinstance(target, str) and target:
            return {
                "tool_name": "send_input",
                "agent_ids": [target],
                "arguments": {
                    "target": target,
                    "message": _required_subagent_send_input_message(subagent_state, target),
                },
            }
    if state_next_action == "close" and close_agent_ids:
        return {"tool_name": "close_agent", "agent_ids": close_agent_ids, "arguments": {"target": close_agent_ids[0]}}
    if state_next_action == "wait" and wait_agent_ids:
        return {
            "tool_name": "wait_agent",
            "agent_ids": wait_agent_ids,
            "arguments": {"targets": wait_agent_ids, "timeout_ms": 60000},
        }
    if close_agent_ids:
        return {"tool_name": "close_agent", "agent_ids": close_agent_ids, "arguments": {"target": close_agent_ids[0]}}
    if wait_agent_ids:
        return {
            "tool_name": "wait_agent",
            "agent_ids": wait_agent_ids,
            "arguments": {"targets": wait_agent_ids, "timeout_ms": 60000},
        }
    return None


def _required_subagent_send_input_message(subagent_state: Any, target: str) -> str:
    agent = getattr(subagent_state, "agents", {}).get(target) if subagent_state is not None else None
    prompt = getattr(agent, "prompt", "") if agent is not None else ""
    if isinstance(prompt, str) and prompt.strip():
        return (
            "Your previous completed result had empty visible output. "
            "Return exactly the output requested in your original prompt, with no prose or markdown.\n"
            f"Original prompt:\n{prompt.strip()}"
        )
    return (
        "Your previous completed result had empty visible output. "
        "Return the exact output requested in your original prompt, with no prose or markdown."
    )


def _contains_response_function_call(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_response_function_call(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    if value.get("type") == "function_call":
        return True
    tool_calls = value.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return True
    return any(_contains_response_function_call(item) for item in value.values())


def _response_output_is_text_or_empty(output: Any) -> bool:
    if output is None:
        return True
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, Mapping):
            return False
        item_type = item.get("type")
        if item_type not in {"message", "reasoning"}:
            return False
    return True


def _response_events_are_text_or_empty(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        event_type = event.get("type")
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type not in {"message", "reasoning"}:
                return False
        elif event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, Mapping) and not _response_output_is_text_or_empty(response.get("output")):
                return False
        elif event_type in {"response.failed", "response.incomplete", "error"}:
            return False
    return True


def _required_subagent_call_item(spec: Mapping[str, Any], call_id: str | None = None) -> dict[str, Any]:
    tool_name = spec.get("tool_name")
    if not isinstance(tool_name, str) or tool_name not in MULTI_AGENT_TOOL_NAMES:
        tool_name = "wait_agent"
    arguments = spec.get("arguments")
    if not isinstance(arguments, Mapping):
        arguments = {}
    call_id = call_id or f"call_codexhub_required_{tool_name}_{uuid.uuid4().hex[:12]}"
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "namespace": "multi_agent_v1",
        "name": tool_name,
        "arguments": json.dumps(dict(arguments), ensure_ascii=True, separators=(",", ":")),
    }


def _with_preserved_spawn_agent_type(
    arguments: Mapping[str, Any],
    original_arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rewritten = dict(arguments)
    agent_type = original_arguments.get("agent_type") if original_arguments is not None else None
    if agent_type in {"worker", "general"}:
        rewritten["agent_type"] = agent_type
    return rewritten


def _required_subagent_call_item_like(spec: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    if spec.get("tool_name") == "spawn_agent":
        original_arguments = _json_object_from_arguments(value.get("arguments"))
        spec = dict(spec)
        required_arguments = spec.get("arguments")
        spec["arguments"] = _with_preserved_spawn_agent_type(
            dict(required_arguments) if isinstance(required_arguments, Mapping) else {},
            original_arguments,
        )
    call_id = value.get("call_id")
    item = _required_subagent_call_item(spec, call_id=call_id if isinstance(call_id, str) and call_id else None)
    item_id = value.get("id")
    if isinstance(item_id, str) and item_id:
        item["id"] = item_id
    status = value.get("status")
    if isinstance(status, str) and status:
        item["status"] = status
    if item.get("status") == "in_progress":
        item["arguments"] = ""
    return item


def _validate_generated_required_spawn_call(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    validated_call_ids: set[str],
) -> None:
    raw_arguments = value.get("arguments")
    if (
        _multi_agent_function_call_name(value) != "spawn_agent"
        or raw_arguments in (None, "")
        or _json_object_from_arguments(raw_arguments) is None
    ):
        return
    identities = [identity for identity in (value.get("call_id"), value.get("id")) if isinstance(identity, str)]
    if any(identity in validated_call_ids for identity in identities):
        return
    _validate_external_worker_selectors(value, event_context, surface=surface)
    validated_call_ids.update(identity for identity in identities if identity)


def _coerce_required_subagent_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str = "coerce",
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return value, False

    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return value, False
    if spec.get("tool_name") == "spawn_agent":
        prompts = (event_context or {}).get("subagent_exact_spawn_prompts")
        if isinstance(prompts, list) and len([prompt for prompt in prompts if isinstance(prompt, str) and prompt]) > 1:
            return value, False
    coerced_item_ids: set[str]
    if isinstance(event_context, dict):
        stored = event_context.setdefault("_required_subagent_coerced_item_ids", set())
        coerced_item_ids = stored if isinstance(stored, set) else set()
        event_context["_required_subagent_coerced_item_ids"] = coerced_item_ids
        stored_generated = event_context.setdefault("_required_subagent_generated_spawn_item_ids", set())
        generated_spawn_item_ids = stored_generated if isinstance(stored_generated, set) else set()
        event_context["_required_subagent_generated_spawn_item_ids"] = generated_spawn_item_ids
        stored_validated = event_context.setdefault("_required_subagent_validated_generated_spawn_ids", set())
        validated_generated_spawn_ids = stored_validated if isinstance(stored_validated, set) else set()
        event_context["_required_subagent_validated_generated_spawn_ids"] = validated_generated_spawn_ids
    else:
        coerced_item_ids = set()
        generated_spawn_item_ids = set()
        validated_generated_spawn_ids = set()
    rewritten, changed = _coerce_required_subagent_tool_calls_inner(
        value,
        spec,
        coerced_item_ids,
        generated_spawn_item_ids,
        event_context,
        surface,
        validated_generated_spawn_ids,
    )
    if changed:
        _write_required_subagent_repair_event(event_context, spec, surface="coerce")
    return rewritten, changed


def _coerce_exact_spawn_prompt_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return value, False
    context = event_context or {}
    prompts = context.get("subagent_exact_spawn_prompts")
    if not isinstance(prompts, list):
        return value, False
    exact_prompts = [prompt for prompt in prompts if isinstance(prompt, str) and prompt]
    if not exact_prompts:
        return value, False
    try:
        offset = int(context.get("subagent_exact_spawn_offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    specs = [{"message": prompt, "fork_context": False} for prompt in exact_prompts[max(0, offset) :]]
    if not specs:
        return value, False
    if isinstance(event_context, dict):
        state_key = "_exact_spawn_prompt_coerce_state"
        stored_state = event_context.get(state_key)
        if not isinstance(stored_state, dict):
            stored_state = {}
            event_context[state_key] = stored_state
        signature = {"prompts": exact_prompts, "offset": max(0, offset)}
        if stored_state.get("signature") != signature:
            stored_state.clear()
            stored_state["signature"] = signature
            stored_state["next_index"] = 0
            stored_state["arguments_by_item_id"] = {}
        state = stored_state
    else:
        state = {"next_index": 0, "arguments_by_item_id": {}}
    rewritten, changed = _coerce_exact_spawn_prompt_tool_calls_inner(value, specs, state)
    if changed:
        _write_required_subagent_repair_event(
            event_context,
            {"tool_name": "spawn_agent", "agent_ids": []},
            surface="exact_prompt_coerce",
        )
    return rewritten, changed


def _coerce_exact_spawn_prompt_tool_calls_inner(
    value: Any,
    specs: list[Mapping[str, Any]],
    state: dict[str, Any],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _coerce_exact_spawn_prompt_tool_calls_inner(item, specs, state)
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if value.get("type") == "response.function_call_arguments.done":
        item_id = value.get("item_id")
        arguments_by_item_id = state.get("arguments_by_item_id")
        if not isinstance(item_id, str) or not isinstance(arguments_by_item_id, dict):
            return value, False
        expected = arguments_by_item_id.get(item_id)
        if not isinstance(expected, str):
            return value, False
        original_arguments = _json_object_from_arguments(value.get("arguments"))
        expected_arguments = _json_object_from_arguments(expected)
        if expected_arguments is not None:
            expected_arguments = _with_preserved_spawn_agent_type(expected_arguments, original_arguments)
            expected = json.dumps(expected_arguments, ensure_ascii=True, separators=(",", ":"))
            arguments_by_item_id[item_id] = expected
        if value.get("arguments") == expected:
            return value, False
        rewritten = dict(value)
        rewritten["arguments"] = expected
        return rewritten, True

    if _is_multi_agent_spawn_function_call(value):
        item_id = value.get("id")
        arguments_by_item_id = state.setdefault("arguments_by_item_id", {})
        expected_arguments: Mapping[str, Any] | None = None
        if isinstance(item_id, str) and isinstance(arguments_by_item_id, dict):
            stored = arguments_by_item_id.get(item_id)
            if isinstance(stored, str):
                parsed = _json_object_from_arguments(stored)
                if parsed is not None:
                    expected_arguments = parsed
        if expected_arguments is None:
            next_index = int(state.get("next_index") or 0)
            if next_index >= len(specs):
                return value, False
            expected_arguments = specs[next_index]
            state["next_index"] = next_index + 1
        original_arguments = _json_object_from_arguments(value.get("arguments"))
        expected_arguments = _with_preserved_spawn_agent_type(expected_arguments, original_arguments)
        expected_json = json.dumps(dict(expected_arguments), ensure_ascii=True, separators=(",", ":"))
        if isinstance(item_id, str) and isinstance(arguments_by_item_id, dict):
            arguments_by_item_id[item_id] = expected_json
        rewritten = dict(value)
        rewritten["namespace"] = "multi_agent_v1"
        rewritten["name"] = "spawn_agent"
        if rewritten.get("status") == "in_progress":
            rewritten["arguments"] = ""
        else:
            rewritten["arguments"] = _dump_arguments_like(value.get("arguments"), expected_arguments)
        return (rewritten, True) if rewritten != value else (value, False)

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _coerce_exact_spawn_prompt_tool_calls_inner(item, specs, state)
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _coerce_required_subagent_tool_calls_inner(
    value: Any,
    spec: Mapping[str, Any],
    coerced_item_ids: set[str],
    generated_spawn_item_ids: set[str],
    event_context: Mapping[str, Any] | None,
    surface: str,
    validated_generated_spawn_ids: set[str],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _coerce_required_subagent_tool_calls_inner(
                item,
                spec,
                coerced_item_ids,
                generated_spawn_item_ids,
                event_context,
                surface,
                validated_generated_spawn_ids,
            )
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if value.get("type") == "response.function_call_arguments.done":
        item_id = value.get("item_id")
        if not isinstance(item_id, str) or item_id not in coerced_item_ids:
            return value, False
        arguments = dict(spec.get("arguments")) if isinstance(spec.get("arguments"), Mapping) else {}
        original_arguments = _json_object_from_arguments(value.get("arguments"))
        if spec.get("tool_name") == "spawn_agent" and original_arguments is not None:
            arguments = _with_preserved_spawn_agent_type(arguments, original_arguments)
        expected = json.dumps(dict(arguments), ensure_ascii=True, separators=(",", ":"))
        if spec.get("tool_name") == "spawn_agent" and item_id in generated_spawn_item_ids:
            _validate_generated_required_spawn_call(
                {
                    "type": "function_call",
                    "id": item_id,
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "arguments": expected,
                },
                event_context,
                surface=surface,
                validated_call_ids=validated_generated_spawn_ids,
            )
        if value.get("arguments") != expected:
            rewritten = dict(value)
            rewritten["arguments"] = expected
            return rewritten, True
        return value, False

    original_tool_name = _multi_agent_function_call_name(value)
    if original_tool_name is not None:
        replacement = _required_subagent_call_item_like(spec, value)
        item_id = replacement.get("id")
        if isinstance(item_id, str) and item_id:
            coerced_item_ids.add(item_id)
        if original_tool_name != "spawn_agent" and _multi_agent_function_call_name(replacement) == "spawn_agent":
            if isinstance(item_id, str) and item_id:
                generated_spawn_item_ids.add(item_id)
            _validate_generated_required_spawn_call(
                replacement,
                event_context,
                surface=surface,
                validated_call_ids=validated_generated_spawn_ids,
            )
        return (replacement, True) if replacement != value else (value, False)

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _coerce_required_subagent_tool_calls_inner(
            item,
            spec,
            coerced_item_ids,
            generated_spawn_item_ids,
            event_context,
            surface,
            validated_generated_spawn_ids,
        )
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _required_subagent_call_events(
    spec: Mapping[str, Any],
    response: Mapping[str, Any] | None = None,
    *,
    output_index: int = 0,
) -> list[dict[str, Any]]:
    response_obj = dict(response) if isinstance(response, Mapping) else {}
    call_id = f"call_codexhub_required_{spec.get('tool_name')}_{uuid.uuid4().hex[:12]}"
    item = _required_subagent_call_item(spec, call_id=call_id)
    in_progress_item = dict(item)
    in_progress_item["status"] = "in_progress"
    in_progress_item["arguments"] = ""
    completed_response = {
        "id": response_obj.get("id") if isinstance(response_obj.get("id"), str) else f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "status": "completed",
        "model": response_obj.get("model"),
        "output": [item],
    }
    usage = response_obj.get("usage")
    if isinstance(usage, Mapping):
        completed_response["usage"] = dict(usage)
    return [
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": in_progress_item,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "output_index": output_index,
            "arguments": item["arguments"],
        },
        {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": item,
        },
        {
            "type": "response.completed",
            "response": completed_response,
        },
    ]


def _write_required_subagent_repair_event(
    event_context: Mapping[str, Any] | None,
    spec: Mapping[str, Any],
    *,
    surface: str,
) -> None:
    _write_adapter_event(
        event_context,
        "required_subagent_call_repaired",
        surface=surface,
        tool=spec.get("tool_name") if isinstance(spec.get("tool_name"), str) else None,
        agent_ids=spec.get("agent_ids") if isinstance(spec.get("agent_ids"), list) else None,
    )


def _reject_missing_worker_selector_for_generated_call(
    spec: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    if spec.get("tool_name") == "spawn_agent" and bool((event_context or {}).get("_spawn_selector_required")):
        _raise_worker_contract_error(
            event="worker_selector_validated",
            error_code=WORKER_SELECTOR_ERROR_CODE,
            classification="missing_selector",
            surface=surface,
        )


def _repair_missing_required_subagent_call_payload(
    payload: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return payload, False

    if not isinstance(payload, dict):
        return payload, False
    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return payload, False
    if _contains_response_function_call(payload):
        return payload, False
    if "error" in payload or not _response_output_is_text_or_empty(payload.get("output")):
        return payload, False

    _reject_missing_worker_selector_for_generated_call(spec, event_context, surface="body")
    rewritten = dict(payload)
    rewritten["status"] = "completed"
    rewritten["output"] = [_required_subagent_call_item(spec)]
    _write_required_subagent_repair_event(event_context, spec, surface="body")
    return rewritten, True


def _repair_missing_required_subagent_call_events(
    events: list[Mapping[str, Any]],
    event_context: Mapping[str, Any] | None,
) -> tuple[list[Mapping[str, Any]], bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return events, False

    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return events, False
    if _contains_response_function_call(events) or not _response_events_are_text_or_empty(events):
        return events, False

    completed_response: Mapping[str, Any] | None = None
    for event in events:
        if event.get("type") == "response.completed":
            response = event.get("response")
            completed_response = response if isinstance(response, Mapping) else {}
    if completed_response is None:
        return events, False

    _reject_missing_worker_selector_for_generated_call(spec, event_context, surface="events")
    prefix = [
        dict(event)
        for event in events
        if event.get("type") in {"response.created", "response.in_progress", "response.queued"}
    ]
    repaired = prefix + _required_subagent_call_events(spec, completed_response, output_index=0)
    _write_required_subagent_repair_event(event_context, spec, surface="events")
    return repaired, True


def _repair_missing_required_subagent_call_sse_line(
    payload: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    line_ending: bytes,
) -> bytes | None:
    if not subagent_semantic_repair_enabled(event_context):
        return None

    if payload.get("type") != "response.completed":
        return None
    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return None
    if _contains_response_function_call(payload):
        return None
    response = payload.get("response")
    response_obj = response if isinstance(response, Mapping) else {}
    if not _response_output_is_text_or_empty(response_obj.get("output")):
        return None
    _reject_missing_worker_selector_for_generated_call(spec, event_context, surface="sse")
    output = response_obj.get("output")
    output_index = len(output) if isinstance(output, list) else 0
    events = _required_subagent_call_events(spec, response_obj, output_index=output_index)
    _write_required_subagent_repair_event(event_context, spec, surface="sse")
    return b"".join(_sse_json_line(event, line_ending) + line_ending for event in events)


def _suppressed_duplicate_spawn_message(subagent_state: Any | None) -> dict[str, Any]:
    expected_role = getattr(subagent_state, "next_expected_role", None)
    expected_task = getattr(subagent_state, "next_expected_task", None)
    parts = [
        "required_next_action: the attempted multi_agent_v1.spawn_agent call was suppressed because it repeats an already spawned role/task.",
        "Call multi_agent_v1.spawn_agent for the distinct role/task that is currently expected.",
    ]
    if expected_role:
        parts.append(f"next_expected_role: {expected_role}")
    if expected_task:
        parts.append(f"next_expected_task: {expected_task}")
    return {
        "type": "message",
        "role": "assistant",
        "content": "\n".join(parts),
    }


def _is_multi_agent_spawn_function_call(value: Mapping[str, Any]) -> bool:
    if value.get("type") != "function_call":
        return False
    name = value.get("name")
    namespace = value.get("namespace")
    if namespace == "multi_agent_v1" and name == "spawn_agent":
        return True
    return name == "multi_agent_v1__spawn_agent"


def _replace_embedded_model(body: bytes, model_id: str, upstream_model: str) -> bytes:
    model_token = json.dumps(model_id).encode("utf-8")
    upstream_token = json.dumps(upstream_model).encode("utf-8")

    def replace_match(match: re.Match[bytes]) -> bytes:
        prefix, token = match.group(0).split(b":", 1)
        if token.strip() == model_token:
            return prefix + b":" + upstream_token
        return match.group(0)

    return EMBEDDED_MODEL_RE.sub(replace_match, body)


def official_passthrough_request_body(
    body: bytes,
    payload: Mapping[str, Any] | None,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
) -> bytes:
    if not isinstance(payload, Mapping):
        # Strict official passthrough has no parsed shape to safely rewrite.
        return body

    next_payload = dict(payload)
    upstream_model = upstream.get("upstream_model")
    changed = False
    if isinstance(upstream_model, str) and upstream_model and next_payload.get("model") != upstream_model:
        next_payload["model"] = upstream_model
        changed = True
    service_tier = upstream.get("service_tier")
    if isinstance(service_tier, str) and service_tier and next_payload.get("service_tier") != service_tier:
        next_payload["service_tier"] = service_tier
        changed = True
    if _sanitize_unsupported_compaction_input_items(next_payload):
        changed = True
    if next_payload.get("store") is not False:
        next_payload["store"] = False
        changed = True
    reasoning_changed, reasoning_counts = _sanitize_official_input_reasoning_items(next_payload)
    if reasoning_changed:
        write_proxy_event(
            "official_reasoning_history_sanitized",
            upstream="official",
            reasoning_items_removed=reasoning_counts["removed_non_portable"],
            reasoning_items_kept_official_encrypted=reasoning_counts["kept_official_encrypted"],
        )
        changed = True
    if not changed:
        return body
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _safe_json_mapping(body: bytes) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def transparent_request_body(
    body: bytes,
    payload: Mapping[str, Any] | None,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
) -> bytes:
    upstream_name = upstream.get("name")
    upstream_model = upstream.get("upstream_model")
    official_responses_backend = upstream_name == "official"
    upstream_is_third_party = upstream_name != "official"
    if not isinstance(upstream_model, str) or not upstream_model:
        if isinstance(payload, Mapping):
            next_payload = dict(payload)
            changed = False
            if _normalize_responses_message_input_items(next_payload):
                changed = True
            if official_responses_backend and _sanitize_unsupported_compaction_input_items(next_payload):
                changed = True
            if upstream_is_third_party and _rewrite_internal_input_items(
                next_payload,
                preserve_standard_function_history=True,
            ):
                changed = True
            if official_responses_backend and "max_output_tokens" in next_payload:
                del next_payload["max_output_tokens"]
                changed = True
            if official_responses_backend and next_payload.get("store") is not False:
                next_payload["store"] = False
                changed = True
            if official_responses_backend and next_payload.get("stream") is not True:
                next_payload["stream"] = True
                changed = True
            if official_responses_backend and _normalize_responses_string_input(next_payload):
                changed = True
            if upstream_name == "ollama_cloud" and _apply_ollama_reasoning_effort_alias(next_payload):
                changed = True
            if changed:
                return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return body
    if not isinstance(payload, Mapping):
        if isinstance(model_id, str) and model_id != upstream_model:
            return _replace_embedded_model(body, model_id, upstream_model)
        return body

    next_payload = dict(payload)
    changed = False
    if next_payload.get("model") != upstream_model:
        next_payload["model"] = upstream_model
        changed = True
    if official_responses_backend and "max_output_tokens" in next_payload:
        del next_payload["max_output_tokens"]
        changed = True
    if official_responses_backend and next_payload.get("store") is not False:
        next_payload["store"] = False
        changed = True
    if official_responses_backend and next_payload.get("stream") is not True:
        next_payload["stream"] = True
        changed = True
    if official_responses_backend and _normalize_responses_string_input(next_payload):
        changed = True
    if official_responses_backend and _sanitize_unsupported_compaction_input_items(next_payload):
        changed = True
    if _normalize_responses_message_input_items(next_payload):
        changed = True
    if upstream_is_third_party and _rewrite_internal_input_items(
        next_payload,
        preserve_standard_function_history=True,
    ):
        changed = True
    if upstream_name == "ollama_cloud" and _apply_ollama_reasoning_effort_alias(next_payload):
        changed = True
    if not changed:
        return body
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _rewrite_transparent_developer_role_messages(
    body: bytes,
    upstream: Mapping[str, Any],
) -> tuple[bytes, int]:
    if upstream.get("supports_developer_role", True) is not False:
        return body, 0
    payload = _safe_json_mapping(body)
    if payload is None:
        return body, 0
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return body, 0
    next_messages: list[Any] = []
    rewritten = 0
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "developer":
            message = {**message, "role": "system"}
            rewritten += 1
        next_messages.append(message)
    if not rewritten:
        return body, 0
    next_payload = dict(payload)
    next_payload["messages"] = next_messages
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"), rewritten


# JSON Schema positions whose values are themselves schemas (or maps/lists of
# schemas). Value positions such as ``default``, ``enum``, ``const``, or
# ``examples`` must never be rewritten, so normalization is schema-aware rather
# than a blind recursive boolean replacement.
_TOOL_SCHEMA_MAP_KEYS = ("properties", "patternProperties", "$defs", "defs", "definitions", "dependentSchemas")
_TOOL_SCHEMA_VALUE_KEYS = (
    "items",
    "additionalItems",
    "additionalProperties",
    "contains",
    "propertyNames",
    "if",
    "then",
    "else",
    "not",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
)
_TOOL_SCHEMA_LIST_KEYS = ("allOf", "anyOf", "oneOf", "prefixItems")


def _normalize_tool_json_schema_items(value: list[Any], state: dict[str, int]) -> list[Any]:
    return [
        _normalize_tool_json_schema(item, state) if isinstance(item, (dict, bool)) else item
        for item in value
    ]


def _normalize_tool_json_schema(node: Any, state: dict[str, int]) -> Any:
    """Replace boolean subschemas with equivalent object forms.

    ``true`` and ``{}`` accept any instance; ``false`` and ``{"not": {}}``
    accept none. Some upstreams (for example Moonshot-flavored validators)
    reject boolean property schemas outright, so transparent routes normalize
    them without changing validation semantics.
    """
    if isinstance(node, bool):
        state["rewritten"] += 1
        return {} if node else {"not": {}}
    if not isinstance(node, dict):
        return node
    next_node = dict(node)
    for key in _TOOL_SCHEMA_MAP_KEYS:
        value = next_node.get(key)
        if isinstance(value, dict):
            next_node[key] = {
                name: _normalize_tool_json_schema(subschema, state) if isinstance(subschema, (dict, bool)) else subschema
                for name, subschema in value.items()
            }
    for key in _TOOL_SCHEMA_VALUE_KEYS:
        value = next_node.get(key)
        if isinstance(value, (dict, bool)):
            next_node[key] = _normalize_tool_json_schema(value, state)
        elif isinstance(value, list):
            next_node[key] = _normalize_tool_json_schema_items(value, state)
    for key in _TOOL_SCHEMA_LIST_KEYS:
        value = next_node.get(key)
        if isinstance(value, list):
            next_node[key] = _normalize_tool_json_schema_items(value, state)
    return next_node


def _normalize_transparent_tool_schema_booleans(body: bytes) -> tuple[bytes, int]:
    payload = _safe_json_mapping(body)
    if payload is None:
        return body, 0
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return body, 0
    state = {"rewritten": 0}
    next_tools: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict):
            function = tool.get("function")
            if isinstance(function, dict):
                parameters = function.get("parameters")
                if isinstance(parameters, (dict, bool)):
                    parameters = _normalize_tool_json_schema(parameters, state)
                    function = {**function, "parameters": parameters}
                    tool = {**tool, "function": function}
        next_tools.append(tool)
    if not state["rewritten"]:
        return body, 0
    next_payload = dict(payload)
    next_payload["tools"] = next_tools
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"), state["rewritten"]


def _is_raw_provider_probe_context(event_context: Mapping[str, Any] | None) -> bool:
    return bool((event_context or {}).get("raw_provider_probe"))


def compatible_request_body(
    body: bytes,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
    event_context: Mapping[str, Any] | None = None,
    inject_codex_tools: bool = True,
    behavior_profile: str = BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
    tool_protocol_override: str | None = None,
    tool_surface_strategy_override: str | None = None,
    native_responses_tool_codec_override: str | None = None,
) -> bytes:
    upstream_name = upstream.get("name")
    official_passthrough = behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    validated_tool_surface_strategy: str | None = None
    if (
        not official_passthrough
        and upstream_name != "official"
    ):
        # Reject malformed configuration before an unparsable external body can
        # bypass the third-party compatibility boundary. Official passthrough
        # never consults the external capability.
        validated_tool_surface_strategy = (
            tool_surface_strategy_override
            if tool_surface_strategy_override is not None
            else _external_tool_surface_strategy(upstream)
        )
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if official_passthrough:
            return body
        upstream_model = upstream.get("upstream_model")
        if isinstance(model_id, str) and isinstance(upstream_model, str) and upstream_model and model_id != upstream_model:
            return _replace_embedded_model(body, model_id, upstream_model)
        return body

    if not isinstance(payload, dict):
        return body

    upstream_model = upstream.get("upstream_model")
    requested_model = payload.get("model")
    requested_reasoning = _requested_reasoning_effort(payload)
    changed = False
    if official_passthrough:
        return official_passthrough_request_body(body, payload, upstream, model_id=model_id)

    collaboration_protocol = _resolve_collaboration_boundary(
        payload,
        event_context,
        surface="request",
    )

    changed = _normalize_responses_message_input_items(payload)
    if upstream_name == "official":
        if _sanitize_official_reasoning_items(payload):
            changed = True
        if _sanitize_unsupported_compaction_input_items(payload):
            changed = True
        if _normalize_responses_string_input(payload):
            changed = True
        if _sanitize_official_system_messages(payload):
            changed = True
        if _sanitize_official_invalid_tool_calls(payload):
            changed = True
        if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
            payload["model"] = upstream_model
            changed = True
        service_tier = upstream.get("service_tier")
        if isinstance(service_tier, str) and service_tier and payload.get("service_tier") != service_tier:
            payload["service_tier"] = service_tier
            changed = True
        # The chatgpt.com/backend-api/codex endpoint requires store=false,
        # forces streaming, and rejects max_output_tokens. Inject/fix these
        # so callers that don't know about Codex's quirks (e.g. ZCode via
        # the Chat Completions gateway) still work.
        if payload.get("store") is not False:
            payload["store"] = False
            changed = True
        if payload.get("stream") is not True:
            payload["stream"] = True
            changed = True
        if "max_output_tokens" in payload:
            del payload["max_output_tokens"]
            changed = True
        if _sanitize_official_system_messages(payload):
            changed = True
        if not changed:
            return body
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    if _strip_reasoning_encrypted_content(payload):
        changed = True

    raw_provider_probe = _is_raw_provider_probe_context(event_context)
    tool_protocol = (
        tool_protocol_override
        if tool_protocol_override is not None
        else _external_tool_protocol(upstream)
    )
    tool_surface_strategy = (
        validated_tool_surface_strategy
        if validated_tool_surface_strategy is not None
        else _external_tool_surface_strategy(upstream)
    )
    collaboration_v2 = collaboration_protocol == _COLLABORATION_V2
    codex_app_external = (
        behavior_profile == BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER
        or (event_context or {}).get("behavior_profile")
        == BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER
    )
    guidance_enabled = subagent_guidance_enabled(event_context)
    semantic_repair_enabled = subagent_semantic_repair_enabled(event_context)
    if isinstance(event_context, dict):
        event_context["tool_protocol"] = tool_protocol
    if not raw_provider_probe and not collaboration_v2:
        _validate_worker_binding_history(payload)
    bounded_tool_search_terminal_calls = (
        {}
        if raw_provider_probe
        else _bounded_empty_tool_search_terminal_calls(payload.get("input"))
    )
    bounded_tool_search_queries = {
        query for query, _count in bounded_tool_search_terminal_calls.values()
    }
    if isinstance(event_context, dict):
        # A flattened ``function_call`` named ``tool_search`` is ambiguous
        # unless this request actually exposed Codex's client-owned search
        # declaration.  Remember that bounded history or an explicit
        # declaration established that ownership; ordinary provider
        # functions with the same name must remain untouched.
        declared_client_tool_search = any(
            isinstance(tool, Mapping)
            and (
                (tool.get("type") == "tool_search" and tool.get("execution") == "client")
                or (
                    tool.get("type") == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["type"]
                    and tool == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL
                )
            )
            for tool in (payload.get("tools") if isinstance(payload.get("tools"), list) else ())
        )
        if bounded_tool_search_queries or declared_client_tool_search:
            event_context["_tool_search_client_owned"] = True
        if bounded_tool_search_queries:
            event_context["_bounded_tool_search_query_digests"] = frozenset(
                _tool_search_query_digest(query) for query in bounded_tool_search_queries
            )
        else:
            event_context.pop("_bounded_tool_search_query_digests", None)
    if _terminalize_bounded_empty_tool_search_misses(payload, bounded_tool_search_terminal_calls):
        for _query, count in bounded_tool_search_terminal_calls.values():
            write_proxy_event(
                "tool_search_empty_miss_bound",
                query_classification=TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION,
                count=count,
                status=TOOL_SEARCH_UNAVAILABLE_STATUS,
            )
        changed = True
    runtime_tool_plan: RuntimeToolCompatibilityPlan | None = None
    if isinstance(event_context, dict) and not raw_provider_probe:
        if _hoist_additional_tools_input_items(payload):
            changed = True
        if _prepare_runtime_tool_compatibility(
            payload,
            upstream,
            tool_protocol,
            event_context,
            native_responses_tool_codec=native_responses_tool_codec_override,
        ):
            changed = True
        runtime_tool_plan = _runtime_tool_compatibility_plan(event_context)
    if raw_provider_probe:
        pass
    elif collaboration_v2:
        # V2 must not run V1 semantic repair, but a third-party structured
        # Responses endpoint still cannot consume Codex's freeform
        # ``apply_patch`` history items. Keep this wire-only inverse adapter
        # active so the next request does not leak ``custom_tool_call`` into
        # an endpoint that only accepts function-call history.
        input_items = payload.get("input")
        if isinstance(input_items, list):
            adapted_items, _adapted_call_ids, history_changed = _adapt_apply_patch_custom_tool_history(
                input_items,
                event_context=event_context,
            )
            if history_changed:
                payload["input"] = adapted_items
                changed = True
        if _rewrite_v2_unsupported_tool_history(
            payload,
            upstream=upstream,
            tool_protocol=tool_protocol,
            compatibility_plan=runtime_tool_plan,
            event_context=event_context,
            upstream_name=upstream_name,
        ):
            changed = True
        if (
            upstream.get("upstream_format") == "chat_completions"
            and _drop_v2_chat_reasoning_history(
                payload,
                event_context=event_context,
                upstream_name=upstream_name,
            )
        ):
            changed = True
    else:
        # ``additional_tools`` is a legacy Codex input carrier. Preserve it
        # byte-for-byte for eager providers; deferred_core alone promotes it
        # so the selected external surface policy can inspect namespaces.
        if tool_surface_strategy == "deferred_core" and _hoist_additional_tools_input_items(payload):
            changed = True
        if tool_protocol in STRUCTURED_TOOL_PROTOCOLS:
            if _rewrite_structured_tool_input_items(
                payload,
                event_context=event_context,
                upstream_name=upstream_name,
                compatibility_plan=runtime_tool_plan,
            ):
                changed = True
        elif tool_protocol == "none":
            tools = payload.get("tools")
            if isinstance(tools, list):
                filtered_tools = [tool for tool in tools if not _is_multi_agent_tool_schema(tool)]
                if len(filtered_tools) != len(tools):
                    payload["tools"] = filtered_tools
                    changed = True
            if _rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name):
                changed = True
        else:
            if _rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name):
                changed = True
    if (
        not raw_provider_probe
        and upstream.get("upstream_format") == "chat_completions"
        and (collaboration_v2 or codex_app_external)
        and _drop_chat_message_phase(
            payload,
            event_context=event_context,
            upstream_name=upstream_name,
        )
    ):
        changed = True
    input_items = payload.get("input")
    subagent_worker_context = (
        not raw_provider_probe
        and not collaboration_v2
        and tool_protocol in {"text_compat", "chat_tools", "responses_structured"}
        and is_worker_subagent_request(input_items)
    )
    # deferred_core intentionally keeps Codex's bounded, explicit discovery
    # entry point. It does not flatten namespace declarations or introduce a
    # broader discovery service; eager remains the #105-compatible surface.
    # Worker subagents retain their established restricted surface.
    include_tool_search = (
        tool_surface_strategy == "deferred_core"
        and not collaboration_v2
        and not subagent_worker_context
    )
    subagent_state = (
        build_subagent_state(input_items)
        if (
            not raw_provider_probe
            and not collaboration_v2
            and not subagent_worker_context
            and tool_protocol in {"text_compat", "chat_tools", "responses_structured"}
        )
        else None
    )
    subagent_state_active = subagent_state is not None and (
        bool(subagent_state.agents) or subagent_state.requested_count is not None
        or bool(getattr(subagent_state, "workflow_intent", False))
        or subagent_state.next_action == "send_input"
    )
    node_repl_single_step_complete = (
        not raw_provider_probe
        and not collaboration_v2
        and _has_completed_single_step_node_repl_context(input_items)
    )
    subagent_workflow_plan_read_complete = (
        not raw_provider_probe
        and subagent_state_active
        and subagent_state is not None
        and bool(getattr(subagent_state, "workflow_intent", False))
        and not bool(getattr(subagent_state, "dynamic_dag_intent", False))
        and _has_node_repl_subagent_plan_read_context(input_items)
    )
    subagent_workflow_plan_read_required = (
        not raw_provider_probe
        and subagent_state_active
        and subagent_state is not None
        and bool(getattr(subagent_state, "workflow_intent", False))
        and not bool(getattr(subagent_state, "dynamic_dag_intent", False))
        and not subagent_workflow_plan_read_complete
        and not bool(getattr(subagent_state, "agents", {}))
    )

    if raw_provider_probe:
        open_agent_ids = []
        wait_agent_ids = []
        close_agent_ids = []
        closed_agent_ids = []
        lifecycle_complete = False
        include_spawn_agent = False
        include_wait_agent = False
        include_close_agent = False
        include_resume_agent = False
        include_send_input = False
        state_hint = None
    elif subagent_worker_context:
        open_agent_ids = []
        wait_agent_ids = []
        close_agent_ids = []
        closed_agent_ids = []
        lifecycle_complete = False
        include_spawn_agent = False
        include_wait_agent = False
        include_close_agent = False
        include_resume_agent = False
        include_send_input = False
        state_hint = None
    elif collaboration_v2:
        open_agent_ids = []
        wait_agent_ids = []
        close_agent_ids = []
        closed_agent_ids = []
        lifecycle_complete = False
        include_spawn_agent = False
        include_wait_agent = False
        include_close_agent = False
        include_resume_agent = False
        include_send_input = False
        state_hint = None
    elif subagent_state_active and subagent_state is not None and guidance_enabled:
        spawned_agent_ids = subagent_state.spawned_agent_ids
        open_agent_ids = subagent_state.open_agent_ids
        wait_agent_ids = subagent_state.wait_agent_ids
        close_agent_ids = subagent_state.close_agent_ids
        closed_agent_ids = subagent_state.closed_agent_ids
        lifecycle_complete = subagent_state.lifecycle_complete
        include_spawn_agent = subagent_state.next_action == "spawn" and not lifecycle_complete
        include_wait_agent = subagent_state.next_action == "wait" and bool(wait_agent_ids)
        include_close_agent = subagent_state.next_action == "close" and bool(close_agent_ids)
        include_resume_agent = subagent_state.next_action == "send_input"
        include_send_input = subagent_state.next_action == "send_input"
        if subagent_workflow_plan_read_required:
            include_spawn_agent = False
            include_wait_agent = False
            include_close_agent = False
            include_resume_agent = False
            include_send_input = False
        state_hint = (
            state_guidance_message(subagent_state)
            if tool_protocol in {"text_compat", "chat_tools", "responses_structured"} or lifecycle_complete
            else None
        )
    elif subagent_state_active and subagent_state is not None:
        spawned_agent_ids = subagent_state.spawned_agent_ids
        open_agent_ids = subagent_state.open_agent_ids
        wait_agent_ids = subagent_state.wait_agent_ids
        close_agent_ids = subagent_state.close_agent_ids
        closed_agent_ids = subagent_state.closed_agent_ids
        lifecycle_complete = False
        include_spawn_agent = True
        include_wait_agent = True
        include_close_agent = True
        include_resume_agent = True
        include_send_input = True
        state_hint = None
    else:
        spawned_agent_ids = _spawned_multi_agent_ids(input_items)
        open_agent_ids = _open_multi_agent_ids(input_items)
        completed_wait_agent_ids = set(_completed_multi_agent_wait_ids(input_items))
        closed_agent_ids = _closed_multi_agent_ids(input_items)
        wait_agent_ids = [agent_id for agent_id in open_agent_ids if agent_id not in completed_wait_agent_ids]
        close_agent_ids = [agent_id for agent_id in open_agent_ids if agent_id in completed_wait_agent_ids]
        has_open_agent = _has_open_multi_agent_context(input_items)
        requested_spawn_count = _requested_multi_agent_spawn_count(input_items)
        single_loop_multi_agent_request = _has_single_loop_multi_agent_request(input_items)
        bounded_multi_agent_request = single_loop_multi_agent_request or requested_spawn_count is not None
        spawn_more_required = (
            requested_spawn_count is not None and len(spawned_agent_ids) < requested_spawn_count
        )
        lifecycle_complete = (
            bounded_multi_agent_request
            and bool(closed_agent_ids)
            and not has_open_agent
            and (requested_spawn_count is None or len(closed_agent_ids) >= requested_spawn_count)
        )
        include_spawn_agent = not has_open_agent
        include_wait_agent = (not has_open_agent) or not open_agent_ids or bool(wait_agent_ids)
        include_close_agent = (not has_open_agent) or not open_agent_ids or bool(close_agent_ids)
        include_resume_agent = True
        include_send_input = True
        if bounded_multi_agent_request:
            include_resume_agent = False
            include_send_input = False
            if spawn_more_required:
                include_spawn_agent = True
                include_wait_agent = False
                include_close_agent = False
            elif not has_open_agent and not closed_agent_ids:
                include_wait_agent = False
                include_close_agent = False
        if lifecycle_complete:
            include_spawn_agent = False
            include_wait_agent = False
            include_close_agent = False
            include_resume_agent = False
            include_send_input = False
            state_hint = _multi_agent_lifecycle_complete_message(closed_agent_ids)
        elif spawn_more_required and spawned_agent_ids:
            state_hint = _multi_agent_spawn_more_message(spawned_agent_ids, requested_spawn_count)
        else:
            state_hint = _multi_agent_current_state_message(wait_agent_ids, close_agent_ids)
    if isinstance(event_context, dict) and not raw_provider_probe:
        if subagent_state is not None:
            event_context["_subagent_state"] = subagent_state
            exact_prompts = _exact_child_prompts_from_request_text(_active_user_request_text(input_items))
            protocol_state = getattr(subagent_state, "protocol_state", None)
            if exact_prompts:
                event_context["subagent_exact_spawn_prompts"] = list(exact_prompts)
                event_context["subagent_exact_spawn_offset"] = (
                    len(getattr(protocol_state, "agents", {}) or {}) if protocol_state is not None else 0
                )
            if (
                protocol_state is not None
                and bool(getattr(subagent_state, "dynamic_dag_intent", False))
                and is_dynamic_dag_request(input_items)
            ):
                workflow = build_dynamic_dag_workflow(input_items, protocol_state)
                legal_actions = compute_allowed_actions(workflow, protocol_state)
                event_context["subagent_dynamic_dag_active"] = True
                event_context["subagent_dynamic_dag_ready_nodes"] = [
                    action.node_id for action in legal_actions if action.tool_name == "spawn_agent" and action.node_id
                ]
                event_context["subagent_assigned_dynamic_nodes"] = [
                    node.node_id for node in workflow.nodes.values() if node.assigned_agent_id
                ]
                event_context["subagent_legal_actions"] = [
                    {
                        "kind": action.kind,
                        "tool_name": action.tool_name,
                        "arguments": dict(action.arguments),
                        "agent_ids": list(action.agent_ids),
                        "node_id": action.node_id,
                    }
                    for action in legal_actions
                ]
                include_spawn_agent = any(action.tool_name == "spawn_agent" for action in legal_actions)
                include_wait_agent = any(action.tool_name == "wait_agent" for action in legal_actions)
                include_close_agent = any(action.tool_name == "close_agent" for action in legal_actions)
                include_send_input = any(action.tool_name == "send_input" for action in legal_actions)
                include_resume_agent = include_send_input
                lifecycle_complete = workflow_complete(workflow, protocol_state)
                if guidance_enabled and isinstance(input_items, list):
                    input_items.append(dynamic_dag_guidance_message(workflow, protocol_state))
                    changed = True
                if len(legal_actions) != 1:
                    event_context.pop("subagent_required_spawn_arguments", None)
            if exact_prompts and protocol_state is not None:
                workflow = bounded_workflow_from_exact_prompts(
                    exact_prompts,
                    assigned_agent_ids=list(protocol_state.agents.keys()),
                )
                legal_actions = compute_allowed_actions(workflow, protocol_state)
                if len(legal_actions) == 1:
                    event_context["subagent_legal_actions"] = [
                        {
                            "kind": legal_actions[0].kind,
                            "tool_name": legal_actions[0].tool_name,
                            "arguments": dict(legal_actions[0].arguments),
                            "agent_ids": list(legal_actions[0].agent_ids),
                            "node_id": legal_actions[0].node_id,
                        }
                    ]
            required_spawn_arguments = _required_spawn_arguments_for_state(input_items, subagent_state)
            if required_spawn_arguments is not None:
                event_context["subagent_required_spawn_arguments"] = required_spawn_arguments
        event_context["subagent_worker_context"] = bool(subagent_worker_context)
        event_context["subagent_open_agent_ids"] = list(open_agent_ids)
        event_context["subagent_wait_agent_ids"] = list(wait_agent_ids)
        event_context["subagent_close_agent_ids"] = list(close_agent_ids)
        event_context["subagent_closed_agent_ids"] = list(closed_agent_ids)
        event_context["subagent_spawn_allowed"] = bool(include_spawn_agent)
        event_context["subagent_lifecycle_complete"] = bool(lifecycle_complete)
        event_context["subagent_workflow_active"] = bool(
            subagent_state_active
            and subagent_state is not None
            and bool(getattr(subagent_state, "workflow_intent", False))
        )
        event_context["subagent_workflow_plan_read_complete"] = bool(subagent_workflow_plan_read_complete)
        event_context["subagent_workflow_plan_read_required"] = bool(subagent_workflow_plan_read_required)
    if guidance_enabled and state_hint is not None and isinstance(input_items, list):
        node_repl_alias = _runtime_alias_for_namespace_child(
            runtime_tool_plan,
            NODE_REPL_NAMESPACE,
            "js",
        )
        if node_repl_alias is not None:
            state_hint = _rewrite_generated_guidance_tool_name(
                state_hint,
                "mcp__node_repl__js",
                node_repl_alias,
            )
        input_items.append(state_hint)
        _write_adapter_event(
            event_context,
            "multi_agent_current_state_guidance_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            wait_agent_ids=wait_agent_ids,
            close_agent_ids=close_agent_ids,
            closed_agent_ids=closed_agent_ids,
            lifecycle_complete=lifecycle_complete,
        )
        changed = True
    if (
        subagent_worker_context
        and guidance_enabled
        and isinstance(input_items, list)
        and not _has_worker_subagent_finalization_guidance(input_items)
    ):
        input_items.append(_worker_subagent_finalization_message())
        _write_adapter_event(
            event_context,
            "worker_subagent_finalization_guidance_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        )
        changed = True
    if node_repl_single_step_complete and isinstance(input_items, list):
        input_items.append(_node_repl_single_step_complete_message())
        _write_adapter_event(
            event_context,
            "node_repl_single_step_complete_guidance_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        )
        changed = True
    if raw_provider_probe:
        if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
            payload["model"] = upstream_model
            changed = True
        if not changed:
            return body
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if (
        (
            runtime_tool_plan is None
            or (
                native_responses_tool_codec_override
                if native_responses_tool_codec_override is not None
                else _external_native_responses_tool_codec(upstream)
            )
            == "strict_apply_patch"
        )
        and
        tool_protocol == "responses_structured"
        and _adapt_native_responses_tool_declarations(
            payload,
            upstream,
            event_context,
            codec=native_responses_tool_codec_override,
        )
    ):
        changed = True
    allow_codex_tools = tool_protocol != "none"
    if inject_codex_tools and allow_codex_tools and not raw_provider_probe and not collaboration_v2:
        if lifecycle_complete:
            if _hide_tools_for_completed_subagent_lifecycle(payload):
                _write_adapter_event(
                    event_context,
                    "subagent_lifecycle_complete_tools_hidden",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                )
                changed = True
        else:
            restrict_to_subagent_coordinator_tools = bool(
                guidance_enabled
                and
                subagent_state_active
                and subagent_state is not None
                and bool(getattr(subagent_state, "workflow_intent", False))
            )
            # Coordinator and worker restrictions deliberately remain narrower
            # than the normal deferred-core surface.
            runtime_plain_tool_search = bool(
                runtime_tool_plan is not None
                and any(
                    entry.family in {"plain_function", "tool_search"}
                    and entry.original_name == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["name"]
                    and entry.disposition != "omit"
                    for entry in runtime_tool_plan.entries
                )
            )
            effective_include_tool_search = (
                include_tool_search
                and (
                    runtime_tool_plan is None
                    or runtime_plain_tool_search
                    # The Gateway owns the deferred-core declaration for
                    # structured Responses/Chat routes.  It is not present
                    # in the caller's initial tool list, so requiring a
                    # pre-existing plan entry would suppress the very
                    # declaration that must be adapted for Chat providers.
                    or tool_protocol in STRUCTURED_TOOL_PROTOCOLS
                )
                and not subagent_worker_context
                and not restrict_to_subagent_coordinator_tools
            )
            include_node_repl_for_subagent_workflow = (
                restrict_to_subagent_coordinator_tools
                and not node_repl_single_step_complete
                and not subagent_workflow_plan_read_complete
                and not bool(getattr(subagent_state, "dynamic_dag_intent", False))
                and not bool(subagent_state.agents if subagent_state is not None else {})
            )
            if subagent_worker_context and _filter_tools_for_subagent_worker(
                payload,
                compatibility_plan=runtime_tool_plan,
            ):
                _write_adapter_event(
                    event_context,
                    "subagent_worker_tools_restricted",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                )
                changed = True
            if restrict_to_subagent_coordinator_tools and _filter_tools_for_subagent_coordinator(
                payload,
                include_node_repl_tools=include_node_repl_for_subagent_workflow,
                compatibility_plan=runtime_tool_plan,
            ):
                _write_adapter_event(
                    event_context,
                    "subagent_coordinator_tools_restricted",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                    include_node_repl_tools=include_node_repl_for_subagent_workflow,
                )
                changed = True
            tool_names_before = _function_tool_names(payload.get("tools"))
            tool_surface_counts: dict[str, int] = {}
            worker_caller_carrier_supported = _worker_caller_carrier_supported(event_context)
            if isinstance(event_context, dict):
                if include_spawn_agent:
                    event_context["_spawn_selector_required"] = True
                else:
                    event_context.pop("_spawn_selector_required", None)
                if include_spawn_agent and worker_caller_carrier_supported:
                    event_context["_worker_binding_required"] = True
                    event_context["_worker_requested_binding"] = {
                        "agent_type": "worker",
                        "model": requested_model,
                        "reasoning": requested_reasoning,
                    }
                else:
                    event_context.pop("_worker_binding_required", None)
                    event_context.pop("_worker_requested_binding", None)
            explicit_tools_injected = _inject_explicit_codex_tools(
                payload,
                include_tool_search=effective_include_tool_search,
                include_multi_agent_tools=not subagent_worker_context,
                include_spawn_agent=include_spawn_agent,
                include_wait_agent=include_wait_agent,
                include_close_agent=include_close_agent,
                include_resume_agent=include_resume_agent,
                include_send_input=include_send_input,
                include_node_repl_tools=(
                    include_node_repl_for_subagent_workflow
                    if restrict_to_subagent_coordinator_tools
                    else not node_repl_single_step_complete
                ),
                include_local_tool_gateway_tools=not subagent_worker_context,
                strip_namespace_tools=runtime_tool_plan is None,
                strip_all_namespace_tools=(
                    runtime_tool_plan is None and tool_surface_strategy == "deferred_core"
                ),
                include_flattened_namespace_tools=(
                    runtime_tool_plan is None and tool_surface_strategy == "eager"
                ),
                tool_surface_counts=tool_surface_counts,
                open_agent_ids=open_agent_ids,
                wait_agent_ids=wait_agent_ids,
                close_agent_ids=close_agent_ids,
                worker_selector_values=(
                    ("worker", "general")
                    if worker_caller_carrier_supported
                    else ("general",)
                ),
            )
            if _restrict_bounded_tool_search_queries(payload, bounded_tool_search_queries):
                changed = True
            write_proxy_event(
                "external_tool_surface_prepared",
                tool_surface_strategy=tool_surface_strategy,
                **tool_surface_counts,
            )
            if explicit_tools_injected:
                added_tool_names = sorted(_function_tool_names(payload.get("tools")) - tool_names_before)
                _write_adapter_event(
                    event_context,
                    "explicit_codex_tools_injected",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                    added_tool_count=len(added_tool_names),
                    added_tool_names=added_tool_names,
                )
                changed = True
            required_tool_choice_name = None
            if subagent_state_active:
                runtime_node_repl_alias = _runtime_alias_for_namespace_child(
                    runtime_tool_plan,
                    NODE_REPL_NAMESPACE,
                    "js",
                )
                required_node_repl_name = runtime_node_repl_alias or "mcp__node_repl__js"
                if (
                    subagent_workflow_plan_read_required
                    and include_node_repl_for_subagent_workflow
                    and (
                        runtime_node_repl_alias is not None
                        or required_node_repl_name in _function_tool_names(payload.get("tools"))
                    )
                ):
                    required_tool_choice_name = required_node_repl_name
                else:
                    required_tool_choice_name = _required_subagent_tool_choice(
                        tool_protocol=tool_protocol,
                        lifecycle_complete=lifecycle_complete,
                        include_spawn_agent=include_spawn_agent,
                        include_wait_agent=include_wait_agent,
                        include_close_agent=include_close_agent,
                        include_resume_agent=include_resume_agent,
                        include_send_input=include_send_input,
                        include_node_repl_for_subagent_workflow=include_node_repl_for_subagent_workflow,
                    )
            if semantic_repair_enabled and _restrict_tools_to_required_tool(payload, required_tool_choice_name):
                required_tool_family, required_tool_disposition = _runtime_required_tool_diagnostics(
                    runtime_tool_plan,
                    required_tool_choice_name,
                )
                write_proxy_event(
                    "required_tool_tools_restricted",
                    tool_choice_required=True,
                    required_tool_family=required_tool_family,
                    required_tool_disposition=required_tool_disposition,
                )
                changed = True
            if semantic_repair_enabled and _set_required_subagent_tool_choice(
                payload,
                required_tool_choice_name,
                event_context=event_context,
                upstream=upstream_name,
            ):
                changed = True
    if runtime_tool_plan is not None and isinstance(payload.get("tools"), list):
        final_declarations = [
            tool
            for tool in payload["tools"]
            if not (
                isinstance(tool, Mapping)
                and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
            )
        ]
        finalized_plan = runtime_tool_plan.with_final_declarations(
            final_declarations,
            tool_choice=payload.get("tool_choice"),
        )
        if finalized_plan is not runtime_tool_plan and isinstance(event_context, dict):
            runtime_tool_plan = finalized_plan
            event_context[_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY] = finalized_plan
    if runtime_tool_plan is not None and _apply_runtime_tool_compatibility_plan(
        payload,
        runtime_tool_plan,
    ):
        changed = True
    model_id = payload.get("model")
    max_output_tokens, context_window_fallback = (
        _catalog_output_limit(model_id) if isinstance(model_id, str) else (None, False)
    )
    if max_output_tokens is not None:
        requested_max_output_tokens = payload.get("max_output_tokens")
        if context_window_fallback and (
            not isinstance(requested_max_output_tokens, int)
            or requested_max_output_tokens >= max_output_tokens
        ):
            if "max_output_tokens" in payload:
                del payload["max_output_tokens"]
                changed = True
        elif not isinstance(requested_max_output_tokens, int) or requested_max_output_tokens > max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens
            changed = True

    if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
        payload["model"] = upstream_model
        changed = True

    upstream_format = upstream.get("upstream_format")
    if (
        "reasoning" in payload
        and upstream_format != "chat_completions"
        and _reasoning_param_is_unsupported(upstream_name, requested_model, upstream_model)
    ):
        del payload["reasoning"]
        _write_adapter_event(
            event_context,
            "unsupported_reasoning_removed",
            upstream=upstream_name,
            model=requested_model if isinstance(requested_model, str) else None,
            upstream_model=upstream_model if isinstance(upstream_model, str) else None,
        )
        changed = True

    if upstream_name == "ollama_cloud":
        if _apply_ollama_reasoning_effort_alias(payload):
            changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _apply_ollama_reasoning_effort_alias(payload: dict[str, Any]) -> bool:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        replacement = OLLAMA_REASONING_EFFORT_ALIASES.get(effort) if isinstance(effort, str) else None
        if replacement is not None:
            reasoning["effort"] = replacement
            return True
        return False
    replacement = OLLAMA_REASONING_EFFORT_ALIASES.get(reasoning) if isinstance(reasoning, str) else None
    if replacement is not None:
        payload["reasoning"] = replacement
        return True
    return False


APPLY_PATCH_FUNCTION_NAME = "apply_patch"
APPLY_PATCH_ADAPTER_EVENT = "third_party_apply_patch_freeform_adapter"
APPLY_PATCH_ADAPTER_ERROR_CODE = "invalid_apply_patch_function_call"
APPLY_PATCH_FUNCTION_CALL_FIELDS = frozenset(
    {"id", "type", "status", "call_id", "name", "arguments"}
)
APPLY_PATCH_HISTORY_ADAPTER_EVENT = "third_party_apply_patch_freeform_history_adapter"
APPLY_PATCH_CUSTOM_TOOL_HISTORY_CALL_FIELDS = frozenset(
    {"type", "status", "call_id", "name", "input"}
)
APPLY_PATCH_CUSTOM_TOOL_HISTORY_OUTPUT_FIELDS = frozenset(
    {"type", "call_id", "output"}
)
APPLY_PATCH_CUSTOM_TOOL_HISTORY_NATIVE_FIELDS = frozenset({"id"})


class _ApplyPatchAdapterFailure(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _apply_patch_adapter_enabled(event_context: Mapping[str, Any] | None) -> bool:
    return not bool(event_context and event_context.get("_apply_patch_adapter_enabled") is False)


def _write_apply_patch_adapter_event(
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    outcome: str,
    count: int = 1,
    reason: str | None = None,
) -> None:
    fields: dict[str, Any] = {"surface": surface, "outcome": outcome, "count": count}
    if reason is not None:
        fields["reason"] = reason
    _write_adapter_event(event_context, APPLY_PATCH_ADAPTER_EVENT, **fields)


def _raise_apply_patch_adapter_failure(
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    reason: str,
) -> None:
    _write_apply_patch_adapter_event(
        event_context,
        surface=surface,
        outcome="rejected",
        reason=reason,
    )
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            APPLY_PATCH_ADAPTER_ERROR_CODE,
            "Third-party apply_patch function call is not an exact freeform patch invocation.",
        )
    )


def _is_apply_patch_function_call(item: Any) -> bool:
    return (
        isinstance(item, Mapping)
        and item.get("type") == "function_call"
        and item.get("name") == APPLY_PATCH_FUNCTION_NAME
    )


def _is_apply_patch_custom_tool_call(item: Any) -> bool:
    return (
        isinstance(item, Mapping)
        and item.get("type") == "custom_tool_call"
        and item.get("name") == APPLY_PATCH_FUNCTION_NAME
    )


def _require_exact_apply_patch_function_call_fields(item: Mapping[str, Any]) -> None:
    if set(item) != APPLY_PATCH_FUNCTION_CALL_FIELDS:
        raise _ApplyPatchAdapterFailure("function_call_fields_not_exact")


def _apply_patch_arguments_text_and_input(arguments: Any) -> tuple[str, str]:
    def unique_object(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if not isinstance(key, str) or key in parsed:
                raise _ApplyPatchAdapterFailure("duplicate_argument_key")
            parsed[key] = value
        return parsed

    def reject_json_constant(_: str) -> None:
        raise _ApplyPatchAdapterFailure("invalid_arguments")

    if isinstance(arguments, Mapping):
        parsed = dict(arguments)
        arguments_text = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"))
    elif isinstance(arguments, str):
        arguments_text = arguments
        try:
            parsed = json.loads(
                arguments,
                object_pairs_hook=unique_object,
                parse_constant=reject_json_constant,
            )
        except _ApplyPatchAdapterFailure:
            raise
        except (TypeError, ValueError):
            raise _ApplyPatchAdapterFailure("invalid_arguments") from None
    else:
        raise _ApplyPatchAdapterFailure("missing_arguments")

    if not isinstance(parsed, dict) or set(parsed) != {"patch"}:
        raise _ApplyPatchAdapterFailure("arguments_not_exact")
    patch = parsed.get("patch")
    if not isinstance(patch, str):
        raise _ApplyPatchAdapterFailure("patch_not_string")
    if not patch.strip():
        raise _ApplyPatchAdapterFailure("patch_empty")
    return arguments_text, patch


def _apply_patch_item_identity(item: Mapping[str, Any]) -> tuple[str, str]:
    item_id = item.get("id")
    call_id = item.get("call_id")
    if not isinstance(item_id, str) or not item_id:
        raise _ApplyPatchAdapterFailure("missing_item_id")
    if not isinstance(call_id, str) or not call_id:
        raise _ApplyPatchAdapterFailure("missing_call_id")
    return item_id, call_id


def _custom_apply_patch_item(item: Mapping[str, Any], patch: str) -> dict[str, Any]:
    rewritten = dict(item)
    rewritten["type"] = "custom_tool_call"
    rewritten["input"] = patch
    rewritten.pop("arguments", None)
    return rewritten


def _write_apply_patch_history_adapter_event(
    event_context: Mapping[str, Any] | None,
    *,
    outcome: str,
    count: int = 1,
) -> None:
    """Emit count-only telemetry for the request-history inverse adapter."""
    _write_adapter_event(
        event_context,
        APPLY_PATCH_HISTORY_ADAPTER_EVENT,
        outcome=outcome,
        count=count,
    )


def _raise_apply_patch_history_adapter_failure(
    event_context: Mapping[str, Any] | None,
) -> None:
    _write_apply_patch_history_adapter_event(event_context, outcome="rejected")
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            APPLY_PATCH_ADAPTER_ERROR_CODE,
            "Third-party apply_patch custom-tool history is not an exact completed pair.",
        )
    )


def _has_exact_apply_patch_custom_tool_history_fields(
    item: Mapping[str, Any],
    required_fields: frozenset[str],
) -> bool:
    fields = set(item)
    if fields == required_fields:
        return True
    return (
        fields == required_fields | APPLY_PATCH_CUSTOM_TOOL_HISTORY_NATIVE_FIELDS
        and isinstance(item.get("id"), str)
        and bool(item["id"])
    )


def _adapt_apply_patch_custom_tool_history(
    input_items: list[Any],
    *,
    event_context: Mapping[str, Any] | None,
) -> tuple[list[Any], set[str], bool]:
    """Reconstruct exact structured apply_patch history for third-party tools.

    The downstream Codex client represents freeform ``apply_patch`` calls as a
    custom-tool call/result pair.  Structured third-party Responses providers
    need that one completed pair in function-call form to retain the call/result
    relationship.  All other custom-tool history stays on the pre-existing
    compatibility path.
    """
    if not _apply_patch_adapter_enabled(event_context):
        return input_items, set(), False

    rewritten_items: list[Any] = []
    pending_call_ids: set[str] = set()
    adapted_call_ids: set[str] = set()
    foreign_call_ids: set[str] = set()
    unmatched_custom_output_ids: set[str] = set()
    adapted = 0
    untouched = 0

    for raw_item in input_items:
        if not isinstance(raw_item, Mapping):
            rewritten_items.append(raw_item)
            continue

        item_type = raw_item.get("type")
        call_id = raw_item.get("call_id")
        if _is_apply_patch_custom_tool_call(raw_item):
            if (
                not _has_exact_apply_patch_custom_tool_history_fields(
                    raw_item,
                    APPLY_PATCH_CUSTOM_TOOL_HISTORY_CALL_FIELDS,
                )
                or raw_item.get("status") != "completed"
                or not isinstance(call_id, str)
                or not call_id
                or not isinstance(raw_item.get("input"), str)
                or not raw_item["input"].strip()
                or call_id in pending_call_ids
                or call_id in adapted_call_ids
                or call_id in foreign_call_ids
                or call_id in unmatched_custom_output_ids
            ):
                _raise_apply_patch_history_adapter_failure(event_context)

            patch = raw_item["input"]
            pending_call_ids.add(call_id)
            adapted_call_ids.add(call_id)
            rewritten_items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": APPLY_PATCH_FUNCTION_NAME,
                    "arguments": json.dumps(
                        {"patch": patch},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                }
            )
            adapted += 1
            continue

        if item_type == "custom_tool_call_output":
            if isinstance(call_id, str) and call_id in pending_call_ids:
                if not _has_exact_apply_patch_custom_tool_history_fields(
                    raw_item,
                    APPLY_PATCH_CUSTOM_TOOL_HISTORY_OUTPUT_FIELDS,
                ):
                    _raise_apply_patch_history_adapter_failure(event_context)
                pending_call_ids.remove(call_id)
                rewritten_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": raw_item["output"],
                    }
                )
                continue
            if isinstance(call_id, str):
                if call_id in adapted_call_ids:
                    _raise_apply_patch_history_adapter_failure(event_context)
                unmatched_custom_output_ids.add(call_id)
            rewritten_items.append(raw_item)
            continue

        if isinstance(call_id, str) and call_id in adapted_call_ids:
            _raise_apply_patch_history_adapter_failure(event_context)

        if item_type in {"function_call", "function_call_output"}:
            if isinstance(call_id, str) and call_id:
                foreign_call_ids.add(call_id)
        elif item_type == "custom_tool_call":
            if isinstance(call_id, str) and call_id:
                foreign_call_ids.add(call_id)
            untouched += 1
        rewritten_items.append(raw_item)

    if pending_call_ids:
        _raise_apply_patch_history_adapter_failure(event_context)

    if adapted:
        _write_apply_patch_history_adapter_event(
            event_context,
            outcome="adapted",
            count=adapted,
        )
    if untouched:
        _write_apply_patch_history_adapter_event(
            event_context,
            outcome="untouched",
            count=untouched,
        )
    return rewritten_items, adapted_call_ids, bool(adapted)


def _adapt_third_party_apply_patch_response_body(
    payload: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if not _apply_patch_adapter_enabled(event_context) or not isinstance(payload, dict):
        return payload, False
    output = payload.get("output")
    if not isinstance(output, list):
        return payload, False

    adapted = 0
    untouched = 0
    seen_item_ids: set[str] = set()
    seen_call_ids: set[str] = set()
    seen_custom_item_ids: set[str] = set()
    seen_custom_call_ids: set[str] = set()
    seen_custom_keys: set[str] = set()
    rewritten_output: list[Any] = []

    for index, raw_item in enumerate(output):
        if _is_apply_patch_function_call(raw_item):
            assert isinstance(raw_item, Mapping)
            try:
                _require_exact_apply_patch_function_call_fields(raw_item)
                item_id, call_id = _apply_patch_item_identity(raw_item)
                _, patch = _apply_patch_arguments_text_and_input(raw_item.get("arguments"))
                if item_id in seen_item_ids or item_id in seen_custom_item_ids:
                    raise _ApplyPatchAdapterFailure("duplicate_item_id")
                if call_id in seen_call_ids or call_id in seen_custom_call_ids:
                    raise _ApplyPatchAdapterFailure("duplicate_call_id")
            except _ApplyPatchAdapterFailure as exc:
                _raise_apply_patch_adapter_failure(event_context, surface="body", reason=exc.reason)
            seen_item_ids.add(item_id)
            seen_call_ids.add(call_id)
            rewritten_output.append(_custom_apply_patch_item(raw_item, patch))
            adapted += 1
            continue

        if _is_apply_patch_custom_tool_call(raw_item):
            assert isinstance(raw_item, Mapping)
            raw_item_id = raw_item.get("id")
            raw_call_id = raw_item.get("call_id")
            if isinstance(raw_item_id, str) and raw_item_id:
                if raw_item_id in seen_item_ids:
                    _raise_apply_patch_adapter_failure(
                        event_context,
                        surface="body",
                        reason="duplicate_item_id",
                    )
                seen_custom_item_ids.add(raw_item_id)
            if isinstance(raw_call_id, str) and raw_call_id:
                if raw_call_id in seen_call_ids:
                    _raise_apply_patch_adapter_failure(
                        event_context,
                        surface="body",
                        reason="duplicate_call_id",
                    )
                seen_custom_call_ids.add(raw_call_id)
            key = (
                f"item:{raw_item_id}"
                if isinstance(raw_item_id, str) and raw_item_id
                else f"call:{raw_call_id}"
                if isinstance(raw_call_id, str) and raw_call_id
                else f"index:{index}"
            )
            if key not in seen_custom_keys:
                seen_custom_keys.add(key)
                untouched += 1
        rewritten_output.append(raw_item)

    if adapted:
        payload = dict(payload)
        payload["output"] = rewritten_output
        _write_apply_patch_adapter_event(
            event_context,
            surface="body",
            outcome="adapted",
            count=adapted,
        )
    if untouched:
        _write_apply_patch_adapter_event(
            event_context,
            surface="body",
            outcome="untouched",
            count=untouched,
        )
    return payload, bool(adapted)


@dataclass
class _ApplyPatchStreamState:
    item_id: str
    call_id: str
    output_index: int
    initial_arguments: str | None
    arguments: str | None = None
    patch: str | None = None
    delta_arguments: str = ""
    arguments_done: bool = False
    item_done: bool = False


class _ThirdPartyApplyPatchStreamAdapter:
    def __init__(self, event_context: Mapping[str, Any] | None, *, surface: str = "stream"):
        self._event_context = event_context
        self._surface = surface
        self._states: dict[str, _ApplyPatchStreamState] = {}
        self._item_id_by_call_id: dict[str, str] = {}
        self._adapted_item_ids: set[str] = set()
        self._custom_item_ids: set[str] = set()
        self._custom_call_ids: set[str] = set()
        self._untouched_keys: set[str] = set()
        self._terminal_seen = False
        self._finished = False

    def _fail(self, reason: str) -> None:
        _raise_apply_patch_adapter_failure(
            self._event_context,
            surface=self._surface,
            reason=reason,
        )

    def _output_index(self, event: Mapping[str, Any]) -> int:
        output_index = event.get("output_index")
        if isinstance(output_index, bool) or not isinstance(output_index, int) or output_index < 0:
            self._fail("missing_output_index")
        return output_index

    def _remember_untouched(self, item: Mapping[str, Any], fallback: str) -> None:
        item_id = item.get("id")
        call_id = item.get("call_id")
        if isinstance(item_id, str) and item_id:
            if item_id in self._states:
                self._fail("duplicate_item_id")
            self._custom_item_ids.add(item_id)
        if isinstance(call_id, str) and call_id:
            if call_id in self._item_id_by_call_id:
                self._fail("duplicate_call_id")
            self._custom_call_ids.add(call_id)
        key = (
            f"item:{item_id}"
            if isinstance(item_id, str) and item_id
            else f"call:{call_id}"
            if isinstance(call_id, str) and call_id
            else fallback
        )
        self._untouched_keys.add(key)

    def _state_from_added_item(
        self,
        item: Mapping[str, Any],
        output_index: int,
    ) -> tuple[_ApplyPatchStreamState, str]:
        try:
            _require_exact_apply_patch_function_call_fields(item)
            item_id, call_id = _apply_patch_item_identity(item)
            arguments = item.get("arguments")
            if isinstance(arguments, str) and not arguments:
                initial_arguments = None
            else:
                initial_arguments, _ = _apply_patch_arguments_text_and_input(arguments)
        except _ApplyPatchAdapterFailure as exc:
            self._fail(exc.reason)
        if item_id in self._states or item_id in self._custom_item_ids:
            self._fail("duplicate_item_added")
        if call_id in self._item_id_by_call_id or call_id in self._custom_call_ids:
            self._fail("duplicate_call_id")
        state = _ApplyPatchStreamState(
            item_id=item_id,
            call_id=call_id,
            output_index=output_index,
            initial_arguments=initial_arguments,
        )
        self._states[item_id] = state
        self._item_id_by_call_id[call_id] = item_id
        return state, ""

    def _state_for_event(self, event: Mapping[str, Any]) -> _ApplyPatchStreamState:
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            self._fail("missing_item_id")
        state = self._states.get(item_id)
        if state is None:
            self._fail("unpaired_stream_event")
        output_index = self._output_index(event)
        if output_index != state.output_index:
            self._fail("conflicting_output_index")
        return state

    def _check_completed_item(
        self,
        item: Mapping[str, Any],
        state: _ApplyPatchStreamState,
    ) -> None:
        try:
            _require_exact_apply_patch_function_call_fields(item)
            item_id, call_id = _apply_patch_item_identity(item)
            arguments, patch = _apply_patch_arguments_text_and_input(item.get("arguments"))
        except _ApplyPatchAdapterFailure as exc:
            self._fail(exc.reason)
        if item_id != state.item_id or call_id != state.call_id:
            self._fail("conflicting_item_identity")
        if not state.arguments_done or state.arguments is None or state.patch is None:
            self._fail("missing_arguments_done")
        if arguments != state.arguments or patch != state.patch:
            self._fail("conflicting_arguments")

    def _rewrite_terminal_response(self, event: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
        response = event.get("response")
        if not isinstance(response, Mapping):
            return event, False
        output = response.get("output")
        if not isinstance(output, list):
            return event, False
        changed = False
        seen_terminal_item_ids: set[str] = set()
        rewritten_output: list[Any] = []
        for index, raw_item in enumerate(output):
            if _is_apply_patch_function_call(raw_item):
                assert isinstance(raw_item, Mapping)
                try:
                    _require_exact_apply_patch_function_call_fields(raw_item)
                    item_id, call_id = _apply_patch_item_identity(raw_item)
                    arguments, patch = _apply_patch_arguments_text_and_input(raw_item.get("arguments"))
                except _ApplyPatchAdapterFailure as exc:
                    self._fail(exc.reason)
                if item_id in seen_terminal_item_ids:
                    self._fail("duplicate_terminal_item")
                seen_terminal_item_ids.add(item_id)
                state = self._states.get(item_id)
                if state is None:
                    self._fail("unpaired_terminal_item")
                if call_id != state.call_id or not state.item_done or state.arguments != arguments or state.patch != patch:
                    self._fail("conflicting_terminal_item")
                rewritten_output.append(_custom_apply_patch_item(raw_item, patch))
                changed = True
                continue
            if _is_apply_patch_custom_tool_call(raw_item):
                assert isinstance(raw_item, Mapping)
                self._remember_untouched(raw_item, f"terminal:{index}")
            rewritten_output.append(raw_item)
        if not changed:
            return event, False
        rewritten_response = dict(response)
        rewritten_response["output"] = rewritten_output
        rewritten_event = dict(event)
        rewritten_event["response"] = rewritten_response
        return rewritten_event, True

    def _ensure_terminal_lifecycle(self) -> None:
        for state in self._states.values():
            if not state.item_done:
                self._fail("incomplete_tool_lifecycle")

    def events_for_event(self, event: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], bool]:
        event_type = event.get("type")
        if self._terminal_seen and isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error"):
            self._fail("post_terminal_semantic_event")

        if event_type == "response.output_item.added":
            item = event.get("item")
            if _is_apply_patch_function_call(item):
                assert isinstance(item, Mapping)
                output_index = self._output_index(event)
                self._state_from_added_item(item, output_index)
                rewritten_event = dict(event)
                rewritten_event["item"] = _custom_apply_patch_item(item, "")
                return [rewritten_event], True
            if _is_apply_patch_custom_tool_call(item):
                assert isinstance(item, Mapping)
                self._remember_untouched(item, "added")
            return [event], False

        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id")
            if isinstance(item_id, str) and item_id in self._states:
                state = self._state_for_event(event)
                delta = event.get("delta")
                if state.arguments_done or state.item_done or not isinstance(delta, str):
                    self._fail("invalid_arguments_delta")
                # Do not expose the third-party JSON wrapper as freeform input.
                # The validated raw patch is emitted only by the matching done event.
                state.delta_arguments += delta
                return [], True
            return [event], False

        if event_type == "response.function_call_arguments.done":
            item_id = event.get("item_id")
            if isinstance(item_id, str) and item_id in self._states:
                state = self._state_for_event(event)
                if state.arguments_done or state.item_done:
                    self._fail("duplicate_arguments_done")
                try:
                    arguments, patch = _apply_patch_arguments_text_and_input(event.get("arguments"))
                except _ApplyPatchAdapterFailure as exc:
                    self._fail(exc.reason)
                if state.initial_arguments is not None and arguments != state.initial_arguments:
                    self._fail("conflicting_arguments")
                if state.delta_arguments and arguments != state.delta_arguments:
                    self._fail("conflicting_arguments")
                state.arguments = arguments
                state.patch = patch
                state.arguments_done = True
                self._adapted_item_ids.add(state.item_id)
                rewritten_event = dict(event)
                rewritten_event["type"] = "response.custom_tool_call_input.done"
                rewritten_event["input"] = patch
                rewritten_event.pop("arguments", None)
                return [rewritten_event], True
            return [event], False

        if event_type == "response.output_item.done":
            item = event.get("item")
            if _is_apply_patch_function_call(item):
                assert isinstance(item, Mapping)
                try:
                    _require_exact_apply_patch_function_call_fields(item)
                    item_id, _ = _apply_patch_item_identity(item)
                except _ApplyPatchAdapterFailure as exc:
                    self._fail(exc.reason)
                state = self._states.get(item_id)
                if state is None:
                    self._fail("unpaired_stream_item")
                if state.item_done:
                    self._fail("duplicate_item_done")
                if self._output_index(event) != state.output_index:
                    self._fail("conflicting_output_index")
                self._check_completed_item(item, state)
                state.item_done = True
                rewritten_event = dict(event)
                rewritten_event["item"] = _custom_apply_patch_item(item, state.patch or "")
                return [rewritten_event], True
            if _is_apply_patch_custom_tool_call(item):
                assert isinstance(item, Mapping)
                self._remember_untouched(item, "done")
            return [event], False

        if isinstance(event_type, str) and event_type in RESPONSES_TERMINAL_EVENT_TYPES:
            rewritten_event, changed = self._rewrite_terminal_response(event)
            self._ensure_terminal_lifecycle()
            self._terminal_seen = True
            return [rewritten_event], changed

        return [event], False

    def finish(self, *, allow_missing_terminal: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        if self._states and not self._terminal_seen:
            if not allow_missing_terminal:
                self._fail("missing_terminal_event")
            self._ensure_terminal_lifecycle()
        if self._adapted_item_ids:
            _write_apply_patch_adapter_event(
                self._event_context,
                surface=self._surface,
                outcome="adapted",
                count=len(self._adapted_item_ids),
            )
        if self._untouched_keys:
            _write_apply_patch_adapter_event(
                self._event_context,
                surface=self._surface,
                outcome="untouched",
                count=len(self._untouched_keys),
            )


def _adapt_third_party_apply_patch_stream_events(
    events: list[Mapping[str, Any]],
    *,
    event_context: Mapping[str, Any] | None = None,
) -> tuple[list[Mapping[str, Any]], bool]:
    if not _apply_patch_adapter_enabled(event_context):
        return events, False
    adapter = _ThirdPartyApplyPatchStreamAdapter(event_context)
    changed = False
    rewritten: list[Mapping[str, Any]] = []
    for event in events:
        event_replacements, event_changed = adapter.events_for_event(event)
        rewritten.extend(event_replacements)
        changed = changed or event_changed
    adapter.finish()
    return (rewritten if changed else events), changed


def compatible_response_body(
    body: bytes,
    upstream_name: str,
    event_context: Mapping[str, Any] | None = None,
) -> bytes:
    if upstream_name == "official" or _is_raw_provider_probe_context(event_context):
        return body

    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body

    collaboration_protocol = _resolve_collaboration_boundary(
        payload,
        event_context,
        surface="response",
    )
    event_context = _collaboration_context_with_protocol(event_context, collaboration_protocol)
    changed = False
    runtime_tool_plan = _runtime_tool_compatibility_plan_for_attempt(event_context)
    if runtime_tool_plan is not None:
        try:
            decoded_payload = runtime_tool_plan.decode_payload(payload)
        except RuntimeToolCompatibilityError as exc:
            _raise_runtime_tool_compatibility_error(exc)
        if decoded_payload != payload:
            payload = decoded_payload
            changed = True
    changed = _hide_reasoning_text(payload) or changed
    payload, apply_patch_changed = _adapt_third_party_apply_patch_response_body(payload, event_context)
    changed = changed or apply_patch_changed
    payload, _ = _apply_external_worker_response_contract(
        payload,
        event_context,
        surface="body",
        attach_sidecars=False,
    )
    payload, alias_changed = _normalize_third_party_tool_call(payload, event_context)
    if alias_changed:
        _write_adapter_event(
            event_context,
            "third_party_tool_call_alias_normalized",
            upstream=upstream_name,
            surface="body",
        )
    changed = changed or alias_changed
    payload, bounded_tool_search_changed = _suppress_bounded_tool_search_calls(payload, event_context)
    changed = changed or bounded_tool_search_changed
    payload, post_final_multi_agent_changed = _suppress_multi_agent_calls_after_lifecycle_final(
        payload,
        event_context,
    )
    changed = changed or post_final_multi_agent_changed
    payload, worker_multi_agent_changed = _suppress_worker_multi_agent_tool_calls(payload, event_context)
    changed = changed or worker_multi_agent_changed
    payload, coordinator_forbidden_changed = _suppress_coordinator_forbidden_tool_calls(payload, event_context)
    changed = changed or coordinator_forbidden_changed
    payload, invalid_tool_changed = _downgrade_invalid_third_party_tool_calls(payload)
    changed = changed or invalid_tool_changed
    payload, duplicate_spawn_changed = _guard_duplicate_multi_agent_spawn_calls(payload, event_context)
    changed = changed or duplicate_spawn_changed
    payload, exact_spawn_changed = _coerce_exact_spawn_prompt_tool_calls(payload, event_context)
    changed = changed or exact_spawn_changed
    payload, required_tool_changed = _coerce_required_subagent_tool_calls(
        payload,
        event_context,
        surface="body",
    )
    changed = changed or required_tool_changed
    payload, required_call_changed = _repair_missing_required_subagent_call_payload(payload, event_context)
    changed = changed or required_call_changed
    payload, requested_binding_changed = _apply_external_worker_response_contract(
        payload,
        event_context,
        surface="body",
        validate_selectors=False,
    )
    changed = changed or requested_binding_changed
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def compatible_sse_line(
    line: bytes,
    upstream_name: str,
    event_context: Mapping[str, Any] | None = None,
) -> bytes:
    if upstream_name == "official" or _is_raw_provider_probe_context(event_context) or not line.startswith(b"data:"):
        return line

    line_ending = _sse_line_ending(line)
    payload_bytes = _sse_payload_bytes(line)
    if payload_bytes is None:
        return line

    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return line

    collaboration_protocol = _resolve_collaboration_boundary(
        payload,
        event_context,
        surface="stream",
    )
    if collaboration_protocol is None and isinstance(event_context, Mapping):
        selected_protocol = event_context.get("collaboration_protocol")
        if selected_protocol in {_COLLABORATION_V1, _COLLABORATION_V2}:
            collaboration_protocol = selected_protocol
    event_context = _collaboration_context_with_protocol(event_context, collaboration_protocol)
    if collaboration_protocol != _COLLABORATION_V2:
        _remember_worker_stream_event(payload, event_context)
    _raise_on_invalid_worker_stream_event(
        payload,
        event_context,
        surface="sse",
    )

    runtime_tool_plan, stream_state = _runtime_tool_compatibility_stream_for_attempt(
        event_context
    )
    if runtime_tool_plan is not None and stream_state is not None:
        try:
            decoded_events = stream_state.decode_events_for_event(payload)
        except RuntimeToolCompatibilityError as exc:
            _raise_runtime_tool_compatibility_error(exc)
        if not decoded_events:
            return b""
        if len(decoded_events) > 1:
            return b"".join(
                _sse_json_line(event, line_ending) + line_ending
                for event in decoded_events
            )
        decoded_payload = decoded_events[0]
        runtime_tool_changed = decoded_payload != payload
        payload = decoded_payload
    else:
        runtime_tool_changed = False

    if _is_raw_reasoning_stream_event(payload):
        return b""

    changed = _hide_reasoning_text(payload) or runtime_tool_changed
    payload, _ = _apply_external_worker_response_contract(
        payload,
        event_context,
        surface="sse",
        attach_sidecars=False,
    )
    payload, alias_changed = _normalize_third_party_tool_call(payload, event_context)
    if alias_changed:
        _write_adapter_event(
            event_context,
            "third_party_tool_call_alias_normalized",
            upstream=upstream_name,
            surface="sse",
        )
    changed = changed or alias_changed
    payload, bounded_tool_search_changed = _suppress_bounded_tool_search_calls(payload, event_context)
    if payload is None:
        return b""
    changed = changed or bounded_tool_search_changed
    payload, post_final_multi_agent_changed = _suppress_multi_agent_calls_after_lifecycle_final(
        payload,
        event_context,
    )
    if payload is None:
        return b""
    changed = changed or post_final_multi_agent_changed
    payload, worker_multi_agent_changed = _suppress_worker_multi_agent_tool_calls(payload, event_context)
    if payload is None:
        return b""
    changed = changed or worker_multi_agent_changed
    payload, coordinator_forbidden_changed = _suppress_coordinator_forbidden_tool_calls(payload, event_context)
    if payload is None:
        return b""
    changed = changed or coordinator_forbidden_changed
    payload, invalid_tool_changed = _downgrade_invalid_third_party_tool_calls(payload)
    changed = changed or invalid_tool_changed
    payload, duplicate_spawn_changed = _guard_duplicate_multi_agent_spawn_calls(payload, event_context)
    changed = changed or duplicate_spawn_changed
    payload, exact_spawn_changed = _coerce_exact_spawn_prompt_tool_calls(payload, event_context)
    changed = changed or exact_spawn_changed
    payload, required_tool_changed = _coerce_required_subagent_tool_calls(
        payload,
        event_context,
        surface="sse",
    )
    changed = changed or required_tool_changed
    payload, requested_binding_changed = _apply_external_worker_response_contract(
        payload,
        event_context,
        surface="sse",
        validate_selectors=False,
        capture_stream_event=False,
    )
    changed = changed or requested_binding_changed
    repaired_line = _repair_missing_required_subagent_call_sse_line(payload, event_context, line_ending)
    if repaired_line is not None:
        return repaired_line
    if not changed:
        return line
    return _sse_json_line(payload, line_ending)


def _retry_identity_from_context(event_context: Mapping[str, Any] | None) -> str | None:
    """Return the private stable retry identity if one exists in the context."""
    if event_context is None:
        return None
    identity = event_context.get("_retry_attempt_identity")
    return identity if isinstance(identity, str) and identity else None


def _redact_identity_in_text(text: str, identity: str | None) -> str:
    if identity and identity in text:
        return text.replace(identity, "[retry_identity_redacted]")
    return text


def safe_upstream_error_detail(exc: BaseException, *, redact_identity: str | None = None) -> str:
    reason = getattr(exc, "reason", None)
    source = reason if reason is not None else exc
    detail = f"{type(source).__name__}: {source}"
    detail = detail.replace("\r", " ").replace("\n", " ")
    if "Bearer " in detail:
        detail = detail.split("Bearer ", 1)[0] + "Bearer [redacted]"
    if redact_identity:
        detail = detail.replace(redact_identity, "[retry_identity_redacted]")
    return detail[:300]


def transport_failure_phase(exc: BaseException | None) -> str | None:
    """Best-effort phase label for failures before an upstream response is relayed."""
    if exc is None:
        return None
    reason = getattr(exc, "reason", None)
    if isinstance(exc, URLError) and isinstance(reason, BaseException):
        nested = transport_failure_phase(reason)
        if nested:
            return nested
    if isinstance(exc, HTTPError):
        return "response_headers"
    if isinstance(exc, ssl.SSLEOFError):
        return "tls_handshake"
    if isinstance(exc, ssl.SSLError):
        return "tls_handshake"
    if isinstance(exc, TimeoutError):
        return "tcp_connect"
    if isinstance(exc, IncompleteRead):
        return "response_headers"
    detail = safe_upstream_error_detail(exc).lower()
    if "unexpected_eof" in detail or "ssleoferror" in detail or "eof occurred in violation" in detail:
        return "tls_handshake"
    if "timed out" in detail or "timeout" in detail or "winerror 10060" in detail:
        return "tcp_connect"
    if "connection reset" in detail or "connectionreseterror" in detail or "winerror 10054" in detail:
        return "request_write"
    if isinstance(exc, (OSError, URLError)):
        return "tcp_connect"
    return None


def _retry_safety_failure_phase(exc: BaseException | None) -> str | None:
    """Phase label used for the request-scoped retry-safety decision.

    This function is conservative: it returns a pre-write phase only when the
    exception itself is a structurally unambiguous instance of that phase.
    Generic TimeoutError, SSLError, OSError, and URLError are ambiguous (they
    can occur during connect, request write, or body read) and therefore return
    ``None`` so the caller-supplied ``failure_phase`` or the ``unknown`` safety
    class is used.  Only specifically inspected exception types carry
    authoritative phase evidence: socket.gaierror (DNS), ConnectionRefusedError
    (TCP connect), and ssl.SSLCertVerificationError (TLS handshake).  Text
    needles in OS-level error messages are never treated as proof because the
    same message can occur after the request has been written.
    """
    if exc is None:
        return None
    if isinstance(exc, (UpstreamStreamIncompleteError, UpstreamStreamErrorEvent)):
        return "stream_body"
    if isinstance(exc, HTTPError):
        return "response_headers"
    if isinstance(exc, IncompleteRead):
        return "response_headers"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ConnectionRefusedError):
        return "tcp_connect"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_handshake"
    if isinstance(exc, URLError):
        nested = _retry_safety_failure_phase(exc.reason)
        if nested is not None:
            return nested
    return None


def _model_access_path_idempotency_guaranteed(model_access_path: tuple[str, ...]) -> bool:
    """Return True when the exact Model Access Path carries an explicit guarantee.

    The default allowlist is empty: no non-Official main-generation POST is
    assumed idempotent unless it is explicitly enrolled here.  This keeps the
    conservative default while providing a single seam for future guarantees.
    """
    return False


def _model_access_path_from_event_context(
    event_context: Mapping[str, Any] | None,
    upstream_name: str,
    upstream_format: str,
) -> tuple[str, ...]:
    model = ""
    behavior_profile = ""
    inbound_format = ""
    if event_context is not None:
        try:
            model = str(event_context.get("model") or "")
        except Exception:
            model = ""
        try:
            behavior_profile = str(event_context.get("behavior_profile") or "")
        except Exception:
            behavior_profile = ""
        try:
            inbound_format = str(event_context.get("_caller_wire_format") or "")
        except Exception:
            inbound_format = ""
    return (upstream_name, model, behavior_profile, upstream_format, inbound_format)


def _ensure_retry_attempt_identity(
    event_context: dict[str, Any] | None,
    request: Request,
    model_access_path: tuple[str, ...],
) -> str | None:
    """Return the stable attempt identity for this logical request.

    The identity is stored in the mutable event context under a private key so
    it is not emitted by _public_event_context.  When the Model Access Path is
    explicitly idempotent, the identity is attached to the upstream request as
    an idempotency key; it is never logged, returned to the client, or placed
    in telemetry payloads.
    """
    if event_context is None:
        return None
    identity = event_context.get("_retry_attempt_identity")
    if not isinstance(identity, str):
        identity = uuid.uuid4().hex
        event_context["_retry_attempt_identity"] = identity
    if _model_access_path_idempotency_guaranteed(model_access_path):
        request.headers["X-CodexHub-Retry-Attempt-Identity"] = identity
    return identity


_SUPPRESSED_RETRY_SAFETY_CLASSES = frozenset({
    RETRY_SAFETY_SUPPRESSED_POST_WRITE,
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
    RETRY_SAFETY_UNKNOWN,
})


def _retry_safety_class(
    exc: BaseException,
    *,
    request: Request,
    upstream_name: str,
    request_kind: str,
    downstream_exposed: bool,
    model_access_path: tuple[str, ...],
    failure_phase: str | None = None,
) -> str:
    """Classify whether this failure is safe to retry for a non-Official main-generation POST.

    Official main-generation POSTs and all other request kinds preserve their
    existing retry behavior and receive no classification here.

    Callers that already know the failure occurred during the response stream
    may pass ``failure_phase`` to override the exception-based heuristic; this
    is required to distinguish a connect-time ``TimeoutError`` from a body-read
    ``TimeoutError``.
    """
    if upstream_name == "official" or request_kind != RETRY_REQUEST_MAIN_GENERATION:
        return RETRY_SAFETY_SAFE_PREWRITE
    if request.get_method() != "POST":
        return RETRY_SAFETY_SAFE_PREWRITE
    if downstream_exposed:
        return RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
    if _model_access_path_idempotency_guaranteed(model_access_path):
        return RETRY_SAFETY_GUARANTEED_IDEMPOTENT
    phase = failure_phase if failure_phase is not None else _retry_safety_failure_phase(exc)
    if phase in {"dns", "tcp_connect", "tls_handshake"}:
        return RETRY_SAFETY_SAFE_PREWRITE
    if phase in {"request_write", "response_headers", "stream_body"}:
        return RETRY_SAFETY_SUPPRESSED_POST_WRITE
    return RETRY_SAFETY_UNKNOWN


def _emit_upstream_retry_suppressed_event(
    event_context: Mapping[str, Any] | None,
    *,
    upstream_name: str,
    upstream_format: str,
    request_kind: str,
    attempt: int,
    max_attempts: int,
    exc: BaseException,
    failure_class: str,
    failure_phase: str | None,
    retry_safety_class: str,
) -> None:
    identity = _retry_identity_from_context(event_context)
    detail = safe_upstream_error_detail(exc, redact_identity=identity)
    if isinstance(exc, UpstreamStreamErrorEvent):
        detail = "Upstream SSE error event"
    _write_failure_event(
        event_context,
        "upstream_retry_suppressed",
        upstream=upstream_name,
        provider_id=upstream_name,
        upstream_format=upstream_format,
        request_kind=request_kind,
        retryable=False,
        failure_class=failure_class,
        status=_upstream_retry_status(exc),
        attempt=attempt,
        max_attempts=max_attempts,
        delay_ms=0,
        error=type(exc).__name__,
        detail=detail,
        failure_phase=failure_phase or transport_failure_phase(exc),
        retry_safety_class=retry_safety_class,
        terminal=False,
        downstream_output_started=(
            retry_safety_class == RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
        ),
        retry_forbidden=True,
    )


def _downstream_has_been_exposed(handler: Any) -> bool:
    """Return True if upstream response content has been exposed downstream.

    Exposure includes both bytes already written to the downstream socket and
    upstream content (visible/tool output) that the relay layer has accepted and
    would relay, even if it is still buffered.  Headers alone and metadata-only
    events such as ``response.created`` do not count as exposure.
    """
    seam = _handler_downstream_stream_commit(handler)
    if seam is None:
        return False
    return bool(
        getattr(seam, "_downstream_output_started", False)
        or getattr(seam, "_downstream_content_exposed", False)
    )


def _header_items(headers: Mapping[str, str] | Any) -> list[tuple[str, str]]:
    return [(str(key), str(value)) for key, value in headers.items()]


def _get_header(headers: Mapping[str, str] | Any, name: str) -> str | None:
    wanted = name.lower()
    for key, value in _header_items(headers):
        if key.lower() == wanted:
            return value
    return None


def _truthy_probe_value(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def raw_provider_probe_requested(headers: Mapping[str, str] | Any, path: str) -> bool:
    if _truthy_probe_value(_get_header(headers, "X-CodexHub-Raw-Provider-Probe")):
        return True
    try:
        query_values = parse_qs(urlsplit(path).query, keep_blank_values=True)
    except ValueError:
        return False
    return any(_truthy_probe_value(value) for value in query_values.get("raw_provider_probe", []))


def _header_tokens(headers: Mapping[str, str] | Any, name: str) -> set[str]:
    value = _get_header(headers, name)
    if not value:
        return set()
    return {token.strip().lower() for token in value.split(",") if token.strip()}


def _is_websocket_upgrade(headers: Mapping[str, str] | Any) -> bool:
    upgrade = _get_header(headers, "Upgrade")
    if not upgrade or upgrade.lower() != "websocket":
        return False
    return "upgrade" in _header_tokens(headers, "Connection")


def _websocket_probe_frame_metadata(frame: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "direction": "client_to_proxy",
        "opcode": int(frame.opcode),
        "fin": bool(frame.fin),
        "payload_length": len(frame.payload),
        "appears_json": False,
        "json_top_level_keys": [],
    }
    if frame.opcode == 0x8:
        metadata["close_code"] = int.from_bytes(frame.payload[:2], "big") if len(frame.payload) >= 2 else None
        metadata["close_reason_length"] = max(0, len(frame.payload) - 2)
        return metadata
    if frame.opcode not in {0x1, 0x2}:
        return metadata
    try:
        payload = json.loads(frame.payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return metadata
    metadata["appears_json"] = True
    if isinstance(payload, Mapping):
        metadata["json_top_level_keys"] = sorted(str(key) for key in payload.keys())
    return metadata


def request_context_from_headers(headers: Mapping[str, str] | Any) -> dict[str, str]:
    context: dict[str, str] = {}
    direct_headers = {
        "x-codex-turn-id": "turn_id",
        "x-codex-thread-id": "thread_id",
        "x-codex-session-id": "session_id",
        "x-codex-window-id": "window_id",
        "x-codex-client-id": "client_id",
        "x-request-id": "client_request_id",
        "x-query-id": "query_id",
        "x-session-id": "session_id",
        "x-zcode-trace-id": "trace_id",
    }
    for header_name, field_name in direct_headers.items():
        value = _get_header(headers, header_name)
        if value:
            context[field_name] = value[:200]
            if field_name == "client_id":
                context["client_inference_source"] = "header"

    for header_name in ("x-codex-client-metadata", "x-codex-metadata"):
        value = _get_header(headers, header_name)
        if not value:
            continue
        try:
            metadata = json.loads(value)
        except json.JSONDecodeError:
            continue
        if not isinstance(metadata, dict):
            continue
        for key in (
            "client_id",
            "session_id",
            "thread_id",
            "turn_id",
            "window_id",
            "request_kind",
            "thread_source",
        ):
            item = metadata.get(key)
            if isinstance(item, str) and item and key not in context:
                context[key] = item[:200]
                if key == "client_id":
                    context["client_inference_source"] = "metadata"
    user_agent = _get_header(headers, "User-Agent")
    if user_agent:
        context["user_agent_hash"] = proxy_telemetry.telemetry_hmac(
            RUNTIME_CODEX_DIR,
            b"user-agent",
            user_agent[:500].encode("utf-8", errors="ignore"),
        )
    if "client_id" not in context:
        inferred = _infer_client_id(user_agent)
        if inferred:
            context["client_id"] = inferred
            context["client_inference_source"] = "user_agent"
    context.setdefault("client_id", "unknown")
    context.setdefault("client_inference_source", "unknown")
    return context


def _infer_client_id(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    value = user_agent.lower()
    if "opencode" in value:
        return "opencode"
    if "zcode" in value:
        return "zcode"
    if "omp" in value:
        return "omp"
    if "codex desktop/" in value or "codex-app" in value:
        return "codex-app"
    return None


def _is_codex_app_context(request_context: Mapping[str, str]) -> bool:
    return request_context.get("client_id") == "codex-app"


def _bearer_token(headers: Mapping[str, str] | Any) -> str | None:
    auth_header = _get_header(headers, "Authorization")
    if not auth_header:
        return None
    value = auth_header.strip()
    if not value:
        return None
    if value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return value


def _local_request_authorized(
    headers: Mapping[str, str] | Any,
    request_context: Mapping[str, str],
) -> bool:
    expected_key = gateway_client_key()
    if expected_key is None:
        return True
    token = _bearer_token(headers)
    return bool(token and hmac.compare_digest(token, expected_key))


def _has_explicit_third_party_client_identity(request_context: Mapping[str, str]) -> bool:
    client_id = str(request_context.get("client_id") or "").strip().lower()
    return bool(client_id and client_id not in {"unknown", "codex-app"})


@dataclass(frozen=True)
class ToolExposurePolicy:
    requested_mode: ToolExposureMode
    effective_mode: ToolExposureMode
    capability_state: CapabilityState
    supports_search_tool: bool | None
    proven_tool_subset: tuple[str, ...]
    gateway_schema_injection: bool
    strip_caller_tools: bool


@dataclass(frozen=True)
class VisionPlan:
    policy: str
    action: VisionAction
    network_action: VisionNetworkAction
    input_has_image: bool
    target_accepts_images: bool
    image_proxy_enabled: bool


@dataclass(frozen=True)
class RouteRuntimeFacts:
    """Runtime settings captured once before the pure route decision is built."""

    request_timeout_seconds: int
    request_kind_base_attempts: int
    request_kind_attempts_configured: bool
    failure_expansion_attempts: int
    official_open_attempts: int
    capacity_elapsed_limit_seconds: float
    stream_elapsed_limit_seconds: float
    downstream_retry_notice_enabled: bool
    pre_response_budget_seconds: float


@dataclass(frozen=True)
class RetryExecutionPlan:
    eligibility: CapabilityState
    policy: RetryPolicy
    request_kind: str
    request_timeout_seconds: int
    base_open_attempts: int
    base_relay_attempts: int
    failure_expansion_attempts: int
    request_kind_attempts_configured: bool
    retry_http_errors: bool
    open_attempt_budget: int | None
    capacity_elapsed_limit_seconds: float
    stream_elapsed_limit_seconds: float
    emit_downstream_retry_notice: bool
    pre_response_budget_seconds: float | None
    lifecycle_final_retry_eligible: bool
    empty_completed_max_attempts: int = 2

    def _attempts_for_failure_class(
        self,
        base_attempts: int,
        failure_class: str,
        *,
        stream_failure: bool,
    ) -> int:
        if (
            base_attempts <= 1
            or self.request_kind_attempts_configured
        ):
            return base_attempts
        if failure_class in CAPACITY_RETRY_FAILURE_CLASSES:
            return max(base_attempts, self.failure_expansion_attempts)
        if stream_failure and failure_class == RETRY_FAILURE_QUICK_TRANSIENT:
            return max(base_attempts, self.failure_expansion_attempts)
        return base_attempts

    def open_attempts_for_failure_class(self, failure_class: str) -> int:
        if self.open_attempt_budget is not None:
            return self.base_open_attempts
        return self._attempts_for_failure_class(
            self.base_open_attempts,
            failure_class,
            stream_failure=False,
        )

    def relay_attempts_for_failure_class(
        self,
        failure_class: str,
        *,
        stream_failure: bool,
    ) -> int:
        return self._attempts_for_failure_class(
            self.base_relay_attempts,
            failure_class,
            stream_failure=stream_failure,
        )

    def capacity_elapsed_limit_allows(
        self,
        elapsed_seconds: float,
        delay_seconds: int | float,
    ) -> bool:
        return (
            self.capacity_elapsed_limit_seconds <= 0
            or elapsed_seconds + delay_seconds
            <= self.capacity_elapsed_limit_seconds
        )

    def stream_elapsed_limit_allows(
        self,
        elapsed_seconds: float,
        delay_seconds: int | float,
    ) -> bool:
        return (
            self.stream_elapsed_limit_seconds <= 0
            or elapsed_seconds + delay_seconds
            <= self.stream_elapsed_limit_seconds
        )

    def lifecycle_final_extra_attempts(
        self,
        event_context: Mapping[str, Any] | None,
    ) -> int:
        return int(
            self.lifecycle_final_retry_eligible
            and bool((event_context or {}).get("subagent_lifecycle_complete"))
        )

    def retry_delay_seconds(
        self,
        attempt: int,
        *,
        failure_class: str,
        exc: BaseException | None,
    ) -> int:
        retry_after_seconds = _retry_after_delay_seconds(exc)
        if retry_after_seconds is not None:
            return retry_after_seconds
        if failure_class == RETRY_FAILURE_PROVIDER_THROTTLE:
            index = max(1, attempt) - 1
            if index < len(CAPACITY_RETRY_CADENCE_SECONDS):
                return CAPACITY_RETRY_CADENCE_SECONDS[index]
            return CAPACITY_RETRY_CADENCE_SECONDS[-1]
        return min(max(1, attempt - 1) * 2, 8)

    def pre_response_deadline(self, started_at: float) -> float | None:
        if self.pre_response_budget_seconds is None:
            return None
        return started_at + self.pre_response_budget_seconds

    def new_open_attempt_budget(self) -> dict[str, int] | None:
        if self.open_attempt_budget is None:
            return None
        return {
            "max_attempts": self.open_attempt_budget,
            "attempts_started": 0,
        }

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "eligibility": self.eligibility.value,
            "policy": self.policy.value,
            "request_kind": self.request_kind,
            "request_timeout_seconds": self.request_timeout_seconds,
            "base_open_attempts": self.base_open_attempts,
            "base_relay_attempts": self.base_relay_attempts,
            "failure_expansion_attempts": self.failure_expansion_attempts,
            "retry_http_errors": self.retry_http_errors,
            "open_attempt_budget": self.open_attempt_budget,
            "capacity_elapsed_limit_seconds": (
                self.capacity_elapsed_limit_seconds
            ),
            "stream_elapsed_limit_seconds": (
                self.stream_elapsed_limit_seconds
            ),
            "emit_downstream_retry_notice": (
                self.emit_downstream_retry_notice
            ),
            "pre_response_budget_seconds": (
                self.pre_response_budget_seconds
            ),
            "lifecycle_final_retry_eligible": (
                self.lifecycle_final_retry_eligible
            ),
        }


@dataclass(frozen=True)
class RelayExecutionPlan:
    selected_upstream_format: str
    request_kind: str
    streaming_policy: StreamingPolicy
    usage_policy: UsagePolicy
    response_mutation_policy: MutationPolicy
    sse_mutation_policy: MutationPolicy
    verify_cross_protocol_source: bool
    lifecycle_final_retry_enabled: bool


@dataclass(frozen=True)
class RouteAttemptPlan:
    index: int
    upstream_protocol: RouteProtocol
    selected_upstream_format: str
    endpoint_url: str
    wire_format_adapter: str
    request_conversion_steps: tuple[str, ...]
    request_body_mode: AttemptRequestBodyMode
    authentication_strategy: AuthenticationStrategy
    request_headers: FrozenRequestHeaders
    streaming_policy: StreamingPolicy
    usage_policy: UsagePolicy
    transport_policy: TransportPolicy
    request_mutation_policy: MutationPolicy
    response_mutation_policy: MutationPolicy
    sse_mutation_policy: MutationPolicy
    verify_cross_protocol_source: bool
    retry: RetryExecutionPlan
    tool_protocol: str
    tool_surface_strategy: str
    native_responses_tool_codec: str
    named_mutations: frozenset[RouteMutation]
    fallback_http_statuses: frozenset[int]

    @property
    def mutation_summary(self) -> tuple[RouteMutation, ...]:
        return tuple(sorted(self.named_mutations, key=lambda mutation: mutation.value))

    def allows_protocol_fallback_status(self, status: int | None) -> bool:
        return isinstance(status, int) and status in self.fallback_http_statuses

    def relay_execution_plan(
        self,
        *,
        lifecycle_final_retry_enabled: bool,
    ) -> RelayExecutionPlan:
        return RelayExecutionPlan(
            selected_upstream_format=self.selected_upstream_format,
            request_kind=self.retry.request_kind,
            streaming_policy=self.streaming_policy,
            usage_policy=self.usage_policy,
            response_mutation_policy=self.response_mutation_policy,
            sse_mutation_policy=self.sse_mutation_policy,
            verify_cross_protocol_source=(
                self.verify_cross_protocol_source
            ),
            lifecycle_final_retry_enabled=lifecycle_final_retry_enabled,
        )

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "upstream_protocol": self.upstream_protocol.value,
            "endpoint_url": _safe_route_endpoint_url(self.endpoint_url),
            "wire_format_adapter": self.wire_format_adapter,
            "request_conversion_steps": list(
                self.request_conversion_steps
            ),
            "request_body_mode": self.request_body_mode.value,
            "authentication_strategy": (
                self.authentication_strategy.value
            ),
            "streaming_policy": self.streaming_policy.value,
            "usage_policy": self.usage_policy.value,
            "transport_policy": self.transport_policy.value,
            "request_mutation_policy": (
                self.request_mutation_policy.value
            ),
            "response_mutation_policy": (
                self.response_mutation_policy.value
            ),
            "sse_mutation_policy": self.sse_mutation_policy.value,
            "verify_cross_protocol_source": (
                self.verify_cross_protocol_source
            ),
            "tool_protocol": self.tool_protocol,
            "tool_surface_strategy": self.tool_surface_strategy,
            "native_responses_tool_codec": (
                self.native_responses_tool_codec
            ),
            "retry": self.retry.telemetry_snapshot(),
            "fallback_http_statuses": sorted(
                self.fallback_http_statuses
            ),
            "mutation_summary": [
                mutation.value for mutation in self.mutation_summary
            ],
        }

    def request_body(self, prepared_body: bytes) -> bytes:
        if self.request_body_mode == AttemptRequestBodyMode.PREPARED_DIRECT:
            return prepared_body
        if (
            self.request_body_mode
            == AttemptRequestBodyMode.CONVERT_RESPONSES_TO_CHAT
        ):
            return _responses_request_to_chat_completion_body(
                prepared_body,
                drop_client_transport_fields=True,
                drop_reasoning=True,
            )
        raise UnsupportedRouteProtocolError(
            f"unsupported planned request body mode: {self.request_body_mode}"
        )


@dataclass(frozen=True)
class RouteFailureObservation:
    """Plan-wide facts used only when no executable attempt exists."""

    upstream_protocol: RouteProtocol
    wire_format_adapter: str
    authentication_strategy: AuthenticationStrategy
    request_kind: str
    retry_policy: RetryPolicy
    retry_eligibility: CapabilityState
    usage_policy: UsagePolicy
    streaming_policy: StreamingPolicy
    transport_policy: TransportPolicy
    request_mutation_policy: MutationPolicy
    response_mutation_policy: MutationPolicy
    sse_mutation_policy: MutationPolicy


@dataclass(frozen=True)
class RouteCapabilityBinding:
    source_present: bool
    schema_version: int | None
    provider_id: str | None
    model_id: str | None
    configured_upstream_protocol: RouteProtocol
    binding_upstream_protocol: RouteProtocol
    route_scope_state: CapabilityState
    route_scope_failure_reason: str | None


@dataclass(frozen=True)
class RoutePlan:
    schema_version: str
    provider_id: str
    model_requested: str | None
    canonical_model: str | None
    upstream_model: str | None
    inbound_protocol: RouteProtocol
    configured_upstream_protocol_name: str
    configured_upstream_protocol: RouteProtocol
    protocol_capability_state: CapabilityState
    protocol_failure_reason: str | None
    attempts: tuple[RouteAttemptPlan, ...]
    failure_observation: RouteFailureObservation | None
    capability_manifest_version: str | None
    capability_manifest_hash: str | None
    capability_manifest_state: CapabilityState
    capability_binding: RouteCapabilityBinding
    behavior_profile: str
    caller_request_body_mode: CallerRequestBodyMode
    prepared_request_protocol: RouteProtocol
    codex_semantic_adapter: str
    raw_provider_probe: bool
    request_kind_policy: str
    repair_policy: str
    vision: VisionPlan
    tool_exposure: ToolExposurePolicy
    codex_compatibility_policy: CodexCompatibilityPolicy
    collaboration_backend: CollaborationBackend
    execution_owner: ExecutionOwner
    named_mutations: frozenset[RouteMutation]
    official_http_passthrough: bool
    transparent_metered: bool
    transparent_same_format: bool
    transparent_lightweight_fallback: bool
    transparent_tool_loop_guard: bool

    def __post_init__(self) -> None:
        if bool(self.attempts) == (self.failure_observation is not None):
            raise ValueError(
                "route plan must contain either attempts or one failure observation"
            )

    def _required_failure_observation(self) -> RouteFailureObservation:
        if self.failure_observation is None:
            raise RuntimeError("route plan has neither an attempt nor failure facts")
        return self.failure_observation

    @property
    def mutation_summary(self) -> tuple[RouteMutation, ...]:
        return tuple(sorted(self.named_mutations, key=lambda mutation: mutation.value))

    @property
    def vision_proxy_policy(self) -> str:
        return self.vision.policy

    @property
    def primary_attempt(self) -> RouteAttemptPlan | None:
        return self.attempts[0] if self.attempts else None

    @property
    def request_kind(self) -> str:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.retry.request_kind
            if primary_attempt is not None
            else self._required_failure_observation().request_kind
        )

    @property
    def authentication_strategy(self) -> AuthenticationStrategy:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.authentication_strategy
            if primary_attempt is not None
            else self._required_failure_observation().authentication_strategy
        )

    @property
    def upstream_protocol(self) -> RouteProtocol:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.upstream_protocol
            if primary_attempt is not None
            else self._required_failure_observation().upstream_protocol
        )

    @property
    def selected_upstream_format(self) -> str:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.selected_upstream_format
            if primary_attempt is not None
            else self.configured_upstream_protocol_name
        )

    @property
    def wire_format_adapter(self) -> str:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.wire_format_adapter
            if primary_attempt is not None
            else self._required_failure_observation().wire_format_adapter
        )

    @property
    def retry_policy(self) -> RetryPolicy:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.retry.policy
            if primary_attempt is not None
            else self._required_failure_observation().retry_policy
        )

    @property
    def retry_eligibility(self) -> CapabilityState:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.retry.eligibility
            if primary_attempt is not None
            else self._required_failure_observation().retry_eligibility
        )

    @property
    def usage_policy(self) -> UsagePolicy:
        primary_attempt = self.primary_attempt
        return (
            primary_attempt.usage_policy
            if primary_attempt is not None
            else self._required_failure_observation().usage_policy
        )

    @property
    def streaming_policy(self) -> StreamingPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.streaming_policy
        return self._required_failure_observation().streaming_policy

    @property
    def transport_policy(self) -> TransportPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.transport_policy
        return self._required_failure_observation().transport_policy

    @property
    def request_mutation_policy(self) -> MutationPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.request_mutation_policy
        return self._required_failure_observation().request_mutation_policy

    @property
    def response_mutation_policy(self) -> MutationPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.response_mutation_policy
        return self._required_failure_observation().response_mutation_policy

    @property
    def sse_mutation_policy(self) -> MutationPolicy:
        primary_attempt = self.primary_attempt
        if primary_attempt is not None:
            return primary_attempt.sse_mutation_policy
        return self._required_failure_observation().sse_mutation_policy


def behavior_profile_for_request(
    upstream: Mapping[str, Any],
    request_context: Mapping[str, str],
    *,
    inbound_format: str,
    official_http_passthrough_enabled: bool | None = None,
) -> str:
    if str(upstream.get("name")) != "official":
        return BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY
    if official_http_passthrough_enabled is None:
        official_http_passthrough_enabled = gateway_official_http_passthrough_enabled()
    if (
        official_http_passthrough_enabled
        and inbound_format == "responses"
        and _is_codex_app_context(request_context)
    ):
        return BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    return BEHAVIOR_OFFICIAL_GATEWAY_COMPAT


def _wire_format_adapter(inbound_format: str, upstream_format: str) -> str:
    if inbound_format == upstream_format:
        return WIRE_TRANSPARENT
    if inbound_format == "responses" and upstream_format == "chat_completions":
        return WIRE_RESPONSES_TO_CHAT
    if inbound_format == "chat_completions" and upstream_format == "responses":
        return WIRE_CHAT_TO_RESPONSES
    return WIRE_TRANSPARENT


def _route_protocol(value: Any) -> RouteProtocol:
    try:
        return RouteProtocol(str(value))
    except ValueError:
        return RouteProtocol.UNKNOWN


def _route_provider_id(upstream: Mapping[str, Any], requested_provider: str | None = None) -> str:
    """Return the stable exported provider ID, never the transport name."""

    configured = upstream.get("provider_id") or upstream.get("provider_alias")
    if isinstance(configured, str) and configured.strip():
        return canonical_model_id(configured.strip())
    if requested_provider:
        return canonical_model_id(requested_provider)
    return {
        "official": "openai",
        "ollama_cloud": "ollama-cloud",
        "ollama-cloud": "ollama-cloud",
        "volcengine": "volc",
        "minimax_cn": "minimax-cn",
    }.get(str(upstream.get("name") or ""), canonical_model_id(str(upstream.get("name") or "")))


def _validate_route_identity(
    upstream: Mapping[str, Any],
    *,
    requested_provider: str,
    requested_model: str,
    requested_model_id: str,
) -> tuple[str, str]:
    """Validate that route metadata binds exactly to the requested pair."""

    provider_id = _route_provider_id(upstream, requested_provider or None)
    if requested_provider and provider_id != _route_provider_id({"provider_id": requested_provider}, requested_provider):
        raise _identity_failure(
            "resolved upstream provider does not match the requested provider",
            reason="provider_mismatch",
            provider_id=requested_provider,
            model_slug=requested_model_id,
        )
    upstream_model_value = upstream.get("upstream_model")
    if not isinstance(upstream_model_value, str) or not upstream_model_value.strip():
        raise _identity_failure(
            "resolved upstream is missing an exact upstream_model binding",
            reason="missing_upstream_model",
            provider_id=provider_id or requested_provider or None,
            model_slug=requested_model_id,
        )
    upstream_model = canonical_model_id(upstream_model_value.strip())
    if not upstream_model:
        raise _identity_failure(
            "resolved upstream has an empty upstream_model binding",
            reason="missing_upstream_model",
            provider_id=provider_id or requested_provider or None,
            model_slug=requested_model_id,
        )
    if upstream.get("model_id") is not None:
        configured_model_id = canonical_model_id(str(upstream.get("model_id")))
        if configured_model_id != requested_model_id:
            raise _catalog_failure(
                "resolved upstream model_id contradicts the requested model slug",
                reason="configured_model_mismatch",
                provider_id=provider_id,
                model_slug=requested_model_id,
            )
    elif upstream_model != canonical_model_id(requested_model if requested_provider else requested_model_id):
        raise _catalog_failure(
            "resolved upstream upstream_model contradicts the requested model slug",
            reason="configured_model_mismatch",
            provider_id=provider_id,
            model_slug=requested_model_id,
        )
    return provider_id, upstream_model


def _authentication_strategy(value: Any) -> AuthenticationStrategy:
    try:
        return AuthenticationStrategy(str(value))
    except ValueError:
        return AuthenticationStrategy.UNKNOWN


def _route_endpoint_url(
    upstream: Mapping[str, Any],
    protocol: RouteProtocol,
) -> str:
    base_url = upstream.get("base_url")
    if protocol == RouteProtocol.RESPONSES:
        return (
            _responses_url(upstream, "/v1/responses")
            if isinstance(base_url, str) and base_url
            else "/responses"
        )
    if protocol == RouteProtocol.CHAT_COMPLETIONS:
        return (
            _chat_completions_url(upstream)
            if isinstance(base_url, str) and base_url
            else "/chat/completions"
        )
    raise UnsupportedRouteProtocolError(
        f"planned attempt has no executable upstream protocol: {protocol.value}"
    )


def _safe_route_endpoint_url(endpoint_url: str) -> str:
    """Return endpoint identity without credentials or query secrets.

    Route diagnostics need to distinguish configured endpoints, but failure
    telemetry must never copy userinfo, query parameters, or fragments from a
    provider URL.  The route planner only creates HTTP(S) endpoint URLs (or a
    relative fixture path), so preserve the scheme/host/path and bound the
    result for event payloads.
    """

    value = str(endpoint_url or "")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    def safe_path(path: str) -> str:
        # A provider may legitimately put a credential or signed token in its
        # base URL path.  Keep the common fixed API paths readable, but never
        # persist an arbitrary path segment verbatim in telemetry.
        safe_paths = {
            "/v1",
            "/v2",
            "/responses",
            "/response",
            "/chat/completions",
            "/v1/responses",
            "/v1/response",
            "/v1/chat/completions",
        }
        if path in safe_paths:
            return path
        if not path:
            return ""
        segments = path.split("/")
        return "/".join(
            "" if index == 0 and segment == "" else (
                "" if segment == "" else f"sha256:{hashlib.sha256(segment.encode('utf-8')).hexdigest()[:16]}"
            )
            for index, segment in enumerate(segments)
        )

    # ``urlsplit`` accepts schemeless network-path references such as
    # ``//user:secret@example.test/v1``.  They still have a usable hostname,
    # so sanitize them through the same host/port path below.  For malformed
    # values such as ``user:secret@example.test/v1`` there is no trustworthy
    # authority boundary; never echo the raw value because it can contain
    # credentials.
    if not parsed.netloc:
        path = parsed.path if value.startswith("/") and not value.startswith("//") else ""
        return safe_path(path.split("?", 1)[0].split("#", 1)[0])[:300]
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    except ValueError:
        pass
    return urlunsplit((parsed.scheme, netloc, safe_path(parsed.path), "", ""))[:300]


def _capability_state(value: Any, *, default: CapabilityState) -> CapabilityState:
    try:
        return CapabilityState(str(value))
    except ValueError:
        return default


CAPABILITY_MANIFEST_VERSION_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?"
)
SUPPORTED_CAPABILITY_MANIFEST_VERSIONS = frozenset(
    {"provider-capabilities.v3"}
)
CAPABILITY_MANIFEST_HASH_LENGTHS = {
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
}


def _valid_capability_manifest_version(value: str) -> bool:
    return (
        CAPABILITY_MANIFEST_VERSION_RE.fullmatch(value) is not None
        and value in SUPPORTED_CAPABILITY_MANIFEST_VERSIONS
    )


def _valid_capability_manifest_hash(value: str) -> bool:
    algorithm, separator, digest = value.partition(":")
    expected_length = CAPABILITY_MANIFEST_HASH_LENGTHS.get(algorithm.lower())
    return bool(
        separator
        and expected_length is not None
        and len(digest) == expected_length
        and re.fullmatch(r"[0-9a-fA-F]+", digest)
    )


def _capability_manifest_identity(
    upstream: Mapping[str, Any],
) -> tuple[str | None, str | None, CapabilityState]:
    version_value = upstream.get("capability_manifest_version")
    version = (
        version_value.strip()
        if isinstance(version_value, str) and version_value.strip()
        else None
    )
    hash_value = upstream.get("capability_manifest_hash")
    manifest_hash = (
        hash_value.strip()
        if isinstance(hash_value, str) and hash_value.strip()
        else None
    )
    if not (
        version is not None
        and manifest_hash is not None
        and _valid_capability_manifest_version(version)
        and _valid_capability_manifest_hash(manifest_hash)
    ):
        return None, None, CapabilityState.UNQUALIFIED
    return (
        version,
        manifest_hash,
        _capability_state(
            upstream.get("capability_manifest_state"),
            default=CapabilityState.UNQUALIFIED,
        ),
    )


CAPABILITY_BINDING_SCHEMA_VERSION = 1


def _binding_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_binding_provider(value: str | None) -> str | None:
    if value is None:
        return None
    return canonical_model_id(value).replace("_", "-").lower()


def _canonical_binding_model(value: str | None) -> str | None:
    if value is None:
        return None
    return canonical_model_id(value).lower()


def _route_capability_binding(
    upstream: Mapping[str, Any],
    *,
    configured_upstream_protocol: RouteProtocol,
    provider_id: str,
    model_id: str | None,
) -> RouteCapabilityBinding:
    raw_binding = upstream.get("capability_binding")
    if not isinstance(raw_binding, Mapping):
        return RouteCapabilityBinding(
            source_present=raw_binding is not None,
            schema_version=None,
            provider_id=None,
            model_id=None,
            configured_upstream_protocol=configured_upstream_protocol,
            binding_upstream_protocol=RouteProtocol.UNKNOWN,
            route_scope_state=CapabilityState.UNQUALIFIED,
            route_scope_failure_reason=(
                "malformed_capability_binding"
                if raw_binding is not None
                else "missing_capability_binding"
            ),
        )

    schema_value = raw_binding.get("schema_version")
    schema_version = (
        schema_value
        if isinstance(schema_value, int) and not isinstance(schema_value, bool)
        else None
    )
    binding_provider = _binding_text(raw_binding.get("provider"))
    binding_model = _binding_text(raw_binding.get("model"))
    binding_protocol = _route_protocol(
        raw_binding.get("upstream_protocol")
    )

    route_scope_failure_reason: str | None = None
    if schema_version != CAPABILITY_BINDING_SCHEMA_VERSION:
        route_scope_failure_reason = "unsupported_binding_schema_version"
    elif (
        _canonical_binding_provider(binding_provider)
        != _canonical_binding_provider(provider_id)
    ):
        route_scope_failure_reason = "binding_provider_mismatch"
    elif (
        model_id is not None
        and _canonical_binding_model(binding_model)
        != _canonical_binding_model(model_id)
    ):
        route_scope_failure_reason = "binding_model_mismatch"
    elif binding_protocol != configured_upstream_protocol:
        route_scope_failure_reason = (
            "binding_upstream_protocol_mismatch"
        )

    route_scope_state = (
        CapabilityState.SUPPORTED
        if route_scope_failure_reason is None
        else CapabilityState.UNQUALIFIED
    )
    return RouteCapabilityBinding(
        source_present=True,
        schema_version=schema_version,
        provider_id=binding_provider,
        model_id=binding_model,
        configured_upstream_protocol=configured_upstream_protocol,
        binding_upstream_protocol=binding_protocol,
        route_scope_state=route_scope_state,
        route_scope_failure_reason=route_scope_failure_reason,
    )


def _default_route_runtime_facts(request_kind: str) -> RouteRuntimeFacts:
    return RouteRuntimeFacts(
        request_timeout_seconds=DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
        request_kind_base_attempts=_default_retry_attempts_for_request_kind(
            request_kind
        ),
        request_kind_attempts_configured=False,
        failure_expansion_attempts=DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS,
        official_open_attempts=DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS,
        capacity_elapsed_limit_seconds=(
            DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS
        ),
        stream_elapsed_limit_seconds=DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS,
        downstream_retry_notice_enabled=False,
        pre_response_budget_seconds=(
            DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS
        ),
    )


def _route_runtime_facts(request_kind: str) -> RouteRuntimeFacts:
    return RouteRuntimeFacts(
        request_timeout_seconds=upstream_timeout_seconds(),
        request_kind_base_attempts=_upstream_retry_attempts(request_kind),
        request_kind_attempts_configured=(
            _request_kind_retry_attempts_configured(request_kind)
        ),
        failure_expansion_attempts=gateway_auto_retry_max_attempts(),
        official_open_attempts=official_upstream_open_attempts(),
        capacity_elapsed_limit_seconds=(
            gateway_capacity_retry_elapsed_limit_seconds()
        ),
        stream_elapsed_limit_seconds=(
            gateway_stream_retry_elapsed_limit_seconds()
        ),
        downstream_retry_notice_enabled=(
            gateway_downstream_retry_notice_enabled()
        ),
        pre_response_budget_seconds=(
            DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS
        ),
    )


def _vision_plan_for_route(
    *,
    codex_app_external: bool,
    input_has_image: bool,
    target_accepts_images: bool,
    image_proxy_enabled: bool,
) -> VisionPlan:
    if not input_has_image or target_accepts_images:
        return VisionPlan(
            policy=VISION_PROXY_DISABLED,
            action=VisionAction.PASS_THROUGH,
            network_action=VisionNetworkAction.NONE,
            input_has_image=input_has_image,
            target_accepts_images=target_accepts_images,
            image_proxy_enabled=image_proxy_enabled,
        )
    if image_proxy_enabled:
        return VisionPlan(
            policy=(
                VISION_PROXY_CODEX_APP_ADAPTER
                if codex_app_external
                else VISION_PROXY_TRANSPARENT_OVERLAY
            ),
            action=VisionAction.PROXY,
            network_action=VisionNetworkAction.IMAGE_PROXY,
            input_has_image=True,
            target_accepts_images=False,
            image_proxy_enabled=True,
        )
    return VisionPlan(
        policy=VISION_PROXY_DISABLED,
        action=VisionAction.REJECT,
        network_action=VisionNetworkAction.NONE,
        input_has_image=True,
        target_accepts_images=False,
        image_proxy_enabled=False,
    )


def _route_supports_transparent_metering(
    *,
    upstream_name: str,
    configured_upstream_format: str,
    selected_upstream_format: str,
    inbound_format: str,
    wire_format_adapter: str,
    request_context: Mapping[str, str],
    provider_hint: str | None,
) -> bool:
    if _is_codex_app_context(request_context):
        return False
    explicit_client = _has_explicit_third_party_client_identity(request_context)
    provider_scoped = provider_hint is not None
    standard_external = provider_hint is None and upstream_name != "official" and explicit_client
    official_responses = provider_hint is None and upstream_name == "official" and explicit_client
    if not (provider_scoped or standard_external or official_responses):
        return False

    if official_responses:
        return selected_upstream_format == "responses" and (
            (inbound_format == "responses" and wire_format_adapter == WIRE_TRANSPARENT)
            or (inbound_format == "chat_completions" and wire_format_adapter == WIRE_CHAT_TO_RESPONSES)
        )

    return (
        (
            inbound_format == "chat_completions"
            and selected_upstream_format == "chat_completions"
            and wire_format_adapter == WIRE_TRANSPARENT
        )
        or (
            inbound_format == "chat_completions"
            and configured_upstream_format == "responses"
            and selected_upstream_format == "responses"
            and wire_format_adapter == WIRE_CHAT_TO_RESPONSES
        )
        or (
            inbound_format == "responses"
            and selected_upstream_format == "responses"
            and wire_format_adapter == WIRE_TRANSPARENT
        )
        or (
            inbound_format == "responses"
            and configured_upstream_format == "chat_completions"
            and selected_upstream_format == "chat_completions"
            and wire_format_adapter == WIRE_RESPONSES_TO_CHAT
        )
    )


def _tool_exposure_policy_for_route(
    upstream: Mapping[str, Any],
    *,
    upstream_name: str,
    behavior_profile: str,
    request_kind: str,
    raw_provider_probe: bool,
) -> ToolExposurePolicy:
    tool_protocol = _external_tool_protocol(upstream)
    raw_requested_mode = upstream.get("tool_exposure_mode")
    if raw_requested_mode is None:
        if behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH:
            requested_mode = ToolExposureMode.OFFICIAL_NATIVE
        elif behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
            requested_mode = ToolExposureMode.UNKNOWN
        else:
            requested_mode = ToolExposureMode.CURRENT_COMPATIBILITY
    else:
        try:
            requested_mode = ToolExposureMode(str(raw_requested_mode))
        except ValueError:
            requested_mode = ToolExposureMode.UNKNOWN

    raw_state = upstream.get("tool_capability_state")
    if raw_state is None:
        capability_state = (
            CapabilityState.SUPPORTED
            if requested_mode
            in {
                ToolExposureMode.CURRENT_COMPATIBILITY,
                ToolExposureMode.OFFICIAL_NATIVE,
            }
            else CapabilityState.UNQUALIFIED
        )
    else:
        try:
            capability_state = CapabilityState(str(raw_state))
        except ValueError:
            capability_state = CapabilityState.UNQUALIFIED
    if requested_mode in {
        ToolExposureMode.NATIVE_DEFERRED_SEARCH_CANDIDATE,
        ToolExposureMode.NATIVE_NO_SEARCH_CANDIDATE,
        ToolExposureMode.UNKNOWN,
    }:
        capability_state = CapabilityState.UNQUALIFIED
    elif requested_mode == ToolExposureMode.UNSUPPORTED:
        capability_state = CapabilityState.UNSUPPORTED
    if tool_protocol == "none":
        capability_state = CapabilityState.UNSUPPORTED

    raw_subset = upstream.get("proven_tool_subset")
    proven_tool_subset = (
        tuple(item for item in raw_subset if isinstance(item, str) and item)
        if isinstance(raw_subset, (list, tuple))
        else ()
    )
    supports_search_tool = upstream.get("supports_search_tool")
    if not isinstance(supports_search_tool, bool):
        supports_search_tool = None

    if behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH:
        effective_mode = ToolExposureMode.OFFICIAL_NATIVE
    elif behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
        effective_mode = ToolExposureMode.UNKNOWN
    else:
        # #57/#58 have not authorized either native candidate. Keeping their
        # evidence visible while executing the compatibility mode is the
        # behavior-preserving fail-closed boundary.
        effective_mode = ToolExposureMode.CURRENT_COMPATIBILITY
    if tool_protocol == "none":
        effective_mode = ToolExposureMode.UNSUPPORTED
    gateway_schema_injection = (
        upstream_name != "official"
        and effective_mode == ToolExposureMode.CURRENT_COMPATIBILITY
        and request_kind != RETRY_REQUEST_COMPACT
        and not raw_provider_probe
    )
    strip_caller_tools = (
        request_kind == RETRY_REQUEST_COMPACT
        and behavior_profile
        not in {
            BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
        }
    )
    return ToolExposurePolicy(
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        capability_state=capability_state,
        supports_search_tool=supports_search_tool,
        proven_tool_subset=proven_tool_subset,
        gateway_schema_injection=gateway_schema_injection,
        strip_caller_tools=strip_caller_tools,
    )


def route_plan_for_request(
    upstream: Mapping[str, Any],
    request_context: Mapping[str, str],
    *,
    inbound_format: str,
    provider_hint: str | None = None,
    model_requested: str | None = None,
    canonical_route_model: str | None = None,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
    raw_provider_probe: bool = False,
    input_has_image: bool = False,
    target_accepts_images: bool = True,
    image_proxy_enabled: bool = False,
    official_http_passthrough_enabled: bool = True,
    caller_stream: bool = True,
    runtime_facts: (
        RouteRuntimeFacts
        | Mapping[str, RouteRuntimeFacts]
        | None
    ) = None,
) -> RoutePlan:
    upstream_name = str(upstream.get("name") or "")
    configured_upstream_format = str(upstream.get("upstream_format") or "responses")
    configured_upstream_protocol = _route_protocol(configured_upstream_format)
    if configured_upstream_protocol == RouteProtocol.AUTO:
        attempt_protocols = (
            RouteProtocol.RESPONSES,
            RouteProtocol.CHAT_COMPLETIONS,
        )
    elif configured_upstream_protocol in {
        RouteProtocol.RESPONSES,
        RouteProtocol.CHAT_COMPLETIONS,
    }:
        attempt_protocols = (configured_upstream_protocol,)
    else:
        attempt_protocols = ()
    requested_model_id = (
        canonical_model_id(canonical_route_model or model_requested)
        if canonical_route_model or model_requested
        else ""
    )
    requested_provider, separator, requested_model = (
        requested_model_id.partition("/")
    )
    resolved_provider_id = _route_provider_id(upstream, requested_provider if separator else None)
    resolved_upstream_model: str | None = None
    if requested_model_id:
        resolved_provider_id, resolved_upstream_model = _validate_route_identity(
            upstream,
            requested_provider=requested_provider if separator else resolved_provider_id,
            requested_model=requested_model if separator else requested_model_id,
            requested_model_id=requested_model_id,
        )
    binding_provider_id = str(
        resolved_provider_id
    )
    binding_model_id = str(
        (requested_model if separator else requested_model_id)
        or resolved_upstream_model
        or upstream.get("upstream_model")
        or ""
    )
    capability_binding = _route_capability_binding(
        upstream,
        configured_upstream_protocol=configured_upstream_protocol,
        provider_id=binding_provider_id,
        model_id=binding_model_id or None,
    )
    binding_scope_failed = (
        capability_binding.source_present
        and capability_binding.route_scope_state
        != CapabilityState.SUPPORTED
    )
    if binding_scope_failed:
        attempt_protocols = ()
    if binding_scope_failed:
        protocol_capability_state = CapabilityState.UNQUALIFIED
    elif attempt_protocols:
        protocol_capability_state = CapabilityState.SUPPORTED
    elif configured_upstream_protocol == RouteProtocol.ANTHROPIC_MESSAGES:
        protocol_capability_state = CapabilityState.UNSUPPORTED
    else:
        protocol_capability_state = CapabilityState.UNQUALIFIED
    selected_upstream_format = (
        attempt_protocols[0].value
        if attempt_protocols
        else configured_upstream_format
    )
    wire_adapter = _wire_format_adapter(inbound_format, selected_upstream_format)
    codex_app_external = upstream_name != "official" and _is_codex_app_context(request_context)
    compatibility_external = (
        upstream_name != "official"
        and provider_hint is None
        and not _is_codex_app_context(request_context)
        and not _has_explicit_third_party_client_identity(request_context)
    )
    transparent_metered = _route_supports_transparent_metering(
        upstream_name=upstream_name,
        configured_upstream_format=configured_upstream_format,
        selected_upstream_format=selected_upstream_format,
        inbound_format=inbound_format,
        wire_format_adapter=wire_adapter,
        request_context=request_context,
        provider_hint=provider_hint,
    )
    if codex_app_external:
        behavior_profile = BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER
    elif compatibility_external:
        behavior_profile = BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY
    elif transparent_metered:
        behavior_profile = BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
    else:
        behavior_profile = behavior_profile_for_request(
            upstream,
            request_context,
            inbound_format=inbound_format,
            official_http_passthrough_enabled=official_http_passthrough_enabled,
        )

    official_http_passthrough = (
        behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    )
    transparent_same_format = transparent_metered and wire_adapter == WIRE_TRANSPARENT
    transparent_lightweight_fallback = transparent_metered and wire_adapter in {
        WIRE_CHAT_TO_RESPONSES,
        WIRE_RESPONSES_TO_CHAT,
    }
    codex_semantic_adapter = (
        CODEX_SEMANTIC_EXTERNAL_ADAPTER
        if codex_app_external or compatibility_external
        else CODEX_SEMANTIC_NONE
    )
    effective_request_kind = (
        RETRY_REQUEST_MAIN_GENERATION if transparent_metered else request_kind
    )
    if isinstance(runtime_facts, RouteRuntimeFacts):
        selected_runtime_facts = runtime_facts
    elif isinstance(runtime_facts, Mapping):
        selected_runtime_facts = runtime_facts.get(
            effective_request_kind,
            _default_route_runtime_facts(effective_request_kind),
        )
    else:
        selected_runtime_facts = _default_route_runtime_facts(
            effective_request_kind
        )
    request_kind_policy = (
        REQUEST_KIND_TRANSPARENT if transparent_metered else REQUEST_KIND_GATEWAY
    )
    retry_policy = (
        RetryPolicy.CONSERVATIVE_PRE_OUTPUT
        if transparent_metered
        else RetryPolicy.GATEWAY_FULL
    )
    usage_policy = (
        UsagePolicy.ASYNC_TAP
        if transparent_metered
        else UsagePolicy.SYNC_CAPTURE
    )
    repair_policy = (
        REPAIR_CODEX_SUBAGENT
        if codex_app_external and not raw_provider_probe
        else REPAIR_NONE
    )
    vision = _vision_plan_for_route(
        codex_app_external=codex_app_external,
        input_has_image=input_has_image,
        target_accepts_images=target_accepts_images,
        image_proxy_enabled=image_proxy_enabled,
    )

    tool_exposure = _tool_exposure_policy_for_route(
        upstream,
        upstream_name=upstream_name,
        behavior_profile=behavior_profile,
        request_kind=request_kind,
        raw_provider_probe=raw_provider_probe,
    )
    if official_http_passthrough:
        codex_compatibility_policy = CodexCompatibilityPolicy.OFFICIAL_NATIVE
        collaboration_backend = CollaborationBackend.CODEX_RUNTIME
        streaming_policy = StreamingPolicy.OFFICIAL_PASSTHROUGH
        mutation_policy = MutationPolicy.OFFICIAL_PASSTHROUGH
    elif transparent_metered:
        codex_compatibility_policy = CodexCompatibilityPolicy.NONE
        collaboration_backend = CollaborationBackend.CLIENT_RUNTIME
        streaming_policy = (
            StreamingPolicy.TRANSPARENT
            if transparent_same_format
            else StreamingPolicy.TRANSPARENT_CONVERTED
        )
        mutation_policy = MutationPolicy.TRANSPARENT
    else:
        codex_compatibility_policy = CodexCompatibilityPolicy.CURRENT_COMPATIBILITY
        collaboration_backend = CollaborationBackend.GATEWAY_COMPATIBILITY
        streaming_policy = StreamingPolicy.GATEWAY_ADAPTED
        mutation_policy = MutationPolicy.GATEWAY_COMPATIBILITY

    authentication_strategy = _authentication_strategy(
        upstream.get("auth") or "unknown"
    )
    request_headers = FrozenRequestHeaders.unmaterialized()
    transport_policy = (
        TransportPolicy.OFFICIAL_KEEPALIVE
        if upstream_name == "official"
        else TransportPolicy.STANDARD
    )
    caller_request_body_mode = (
        CallerRequestBodyMode.CONVERT_CHAT_TO_RESPONSES
        if (
            inbound_format == RouteProtocol.CHAT_COMPLETIONS.value
            and not transparent_same_format
        )
        else CallerRequestBodyMode.PRESERVE_CALLER
    )
    base_named_mutations = {RouteMutation.MODEL_ALIAS}
    if tool_exposure.gateway_schema_injection:
        base_named_mutations.add(RouteMutation.HARD_CODED_SCHEMA_INJECTION)
    if codex_semantic_adapter == CODEX_SEMANTIC_EXTERNAL_ADAPTER:
        base_named_mutations.add(RouteMutation.NAMESPACE_FLATTENING)
    if repair_policy != REPAIR_NONE:
        base_named_mutations.add(RouteMutation.SEMANTIC_REPAIR)
    if official_http_passthrough:
        base_named_mutations.add(RouteMutation.OFFICIAL_TOOL_SEARCH_PRESERVATION)
    elif not transparent_metered:
        base_named_mutations.add(RouteMutation.SYNTHETIC_TERMINAL_FAILURE)
    if vision.action == VisionAction.PROXY:
        base_named_mutations.add(RouteMutation.IMAGE_CONTENT_REPLACEMENT)
    elif vision.action == VisionAction.REJECT:
        base_named_mutations.add(RouteMutation.IMAGE_UNSUPPORTED_REJECTION)
    if tool_exposure.strip_caller_tools:
        base_named_mutations.add(RouteMutation.CALLER_TOOL_STRIPPING)
    if not attempt_protocols:
        base_named_mutations.add(RouteMutation.UNSUPPORTED_PROTOCOL_REJECTION)

    attempts: list[RouteAttemptPlan] = []
    for index, attempt_protocol in enumerate(attempt_protocols):
        attempt_wire_adapter = _wire_format_adapter(
            inbound_format,
            attempt_protocol.value,
        )
        request_body_mode = (
            AttemptRequestBodyMode.CONVERT_RESPONSES_TO_CHAT
            if (
                attempt_protocol == RouteProtocol.CHAT_COMPLETIONS
                and not (
                    transparent_same_format
                    and inbound_format
                    == RouteProtocol.CHAT_COMPLETIONS.value
                )
            )
            else AttemptRequestBodyMode.PREPARED_DIRECT
        )
        request_conversion_steps: list[str] = []
        if (
            caller_request_body_mode
            == CallerRequestBodyMode.CONVERT_CHAT_TO_RESPONSES
        ):
            request_conversion_steps.append(WIRE_CHAT_TO_RESPONSES)
        if (
            request_body_mode
            == AttemptRequestBodyMode.CONVERT_RESPONSES_TO_CHAT
        ):
            request_conversion_steps.append(WIRE_RESPONSES_TO_CHAT)
        attempt_mutations = set(base_named_mutations)
        if request_conversion_steps:
            attempt_mutations.add(RouteMutation.WIRE_CONVERSION)
        if official_http_passthrough:
            base_open_attempts = selected_runtime_facts.official_open_attempts
            base_relay_attempts = OFFICIAL_PASSTHROUGH_FIRST_EVENT_ATTEMPTS
            retry_http_errors = False
            open_attempt_budget = selected_runtime_facts.official_open_attempts
        else:
            base_open_attempts = (
                selected_runtime_facts.request_kind_base_attempts
            )
            base_relay_attempts = (
                selected_runtime_facts.request_kind_base_attempts
            )
            retry_http_errors = True
            open_attempt_budget = None
        retry_execution = RetryExecutionPlan(
            eligibility=protocol_capability_state,
            policy=retry_policy,
            request_kind=effective_request_kind,
            request_timeout_seconds=(
                selected_runtime_facts.request_timeout_seconds
            ),
            base_open_attempts=base_open_attempts,
            base_relay_attempts=base_relay_attempts,
            failure_expansion_attempts=(
                selected_runtime_facts.failure_expansion_attempts
            ),
            request_kind_attempts_configured=(
                selected_runtime_facts.request_kind_attempts_configured
            ),
            retry_http_errors=retry_http_errors,
            open_attempt_budget=open_attempt_budget,
            capacity_elapsed_limit_seconds=(
                selected_runtime_facts.capacity_elapsed_limit_seconds
            ),
            stream_elapsed_limit_seconds=(
                selected_runtime_facts.stream_elapsed_limit_seconds
            ),
            emit_downstream_retry_notice=(
                not official_http_passthrough
                and not transparent_metered
                and caller_stream
                and inbound_format == RouteProtocol.RESPONSES.value
                and selected_runtime_facts.downstream_retry_notice_enabled
            ),
            pre_response_budget_seconds=(
                selected_runtime_facts.pre_response_budget_seconds
                if effective_request_kind
                == RETRY_REQUEST_MAIN_GENERATION
                else None
            ),
            lifecycle_final_retry_eligible=(
                not official_http_passthrough
                and repair_policy != REPAIR_NONE
                and effective_request_kind
                == RETRY_REQUEST_MAIN_GENERATION
            ),
        )
        attempt_upstream = {
            **upstream,
            "upstream_format": attempt_protocol.value,
        }
        attempts.append(
            RouteAttemptPlan(
                index=index,
                upstream_protocol=attempt_protocol,
                selected_upstream_format=attempt_protocol.value,
                endpoint_url=_route_endpoint_url(upstream, attempt_protocol),
                wire_format_adapter=attempt_wire_adapter,
                request_conversion_steps=tuple(request_conversion_steps),
                request_body_mode=request_body_mode,
                authentication_strategy=authentication_strategy,
                request_headers=request_headers,
                streaming_policy=streaming_policy,
                usage_policy=usage_policy,
                transport_policy=transport_policy,
                request_mutation_policy=mutation_policy,
                response_mutation_policy=mutation_policy,
                sse_mutation_policy=mutation_policy,
                verify_cross_protocol_source=(
                    attempt_protocol.value != inbound_format
                    and behavior_profile
                    in {
                        BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
                        BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                        BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
                    }
                ),
                retry=retry_execution,
                tool_protocol=_external_tool_protocol(attempt_upstream),
                tool_surface_strategy=(
                    _external_tool_surface_strategy(attempt_upstream)
                    if upstream_name != "official"
                    else "eager"
                ),
                native_responses_tool_codec=(
                    _external_native_responses_tool_codec(attempt_upstream)
                    if upstream_name != "official"
                    else "none"
                ),
                named_mutations=frozenset(attempt_mutations),
                fallback_http_statuses=(
                    frozenset(AUTO_UPSTREAM_PROTOCOL_FALLBACK_STATUSES)
                    if (
                        configured_upstream_protocol == RouteProtocol.AUTO
                        and index == 0
                        and len(attempt_protocols) > 1
                    )
                    else frozenset()
                ),
            )
        )
    named_mutations = frozenset(
        set(base_named_mutations).union(
            *(attempt.named_mutations for attempt in attempts)
        )
    )

    canonical_model = (
        canonical_model_id(canonical_route_model or model_requested)
        if canonical_route_model or model_requested
        else None
    )
    upstream_model = resolved_upstream_model
    (
        manifest_version,
        manifest_hash,
        manifest_state,
    ) = _capability_manifest_identity(
        upstream
    )
    return RoutePlan(
        schema_version=ROUTE_PLAN_SCHEMA_VERSION,
        provider_id=resolved_provider_id,
        model_requested=model_requested,
        canonical_model=canonical_model,
        upstream_model=upstream_model,
        inbound_protocol=_route_protocol(inbound_format),
        configured_upstream_protocol_name=configured_upstream_format,
        configured_upstream_protocol=configured_upstream_protocol,
        protocol_capability_state=protocol_capability_state,
        protocol_failure_reason=(
            None
            if attempts
            else (
                (
                    "unqualified route capability binding: "
                    f"{capability_binding.route_scope_failure_reason}"
                )
                if binding_scope_failed
                else (
                    f"unsupported upstream protocol: {configured_upstream_format}"
                    if protocol_capability_state == CapabilityState.UNSUPPORTED
                    else f"unqualified upstream protocol: {configured_upstream_format}"
                )
            )
        ),
        attempts=tuple(attempts),
        failure_observation=(
            None
            if attempts
            else RouteFailureObservation(
                upstream_protocol=configured_upstream_protocol,
                wire_format_adapter=wire_adapter,
                authentication_strategy=authentication_strategy,
                request_kind=effective_request_kind,
                retry_policy=retry_policy,
                retry_eligibility=protocol_capability_state,
                usage_policy=usage_policy,
                streaming_policy=streaming_policy,
                transport_policy=transport_policy,
                request_mutation_policy=mutation_policy,
                response_mutation_policy=mutation_policy,
                sse_mutation_policy=mutation_policy,
            )
        ),
        capability_manifest_version=manifest_version,
        capability_manifest_hash=manifest_hash,
        capability_manifest_state=manifest_state,
        capability_binding=capability_binding,
        behavior_profile=behavior_profile,
        caller_request_body_mode=caller_request_body_mode,
        prepared_request_protocol=(
            RouteProtocol.RESPONSES
            if (
                caller_request_body_mode
                == CallerRequestBodyMode.CONVERT_CHAT_TO_RESPONSES
            )
            else _route_protocol(inbound_format)
        ),
        codex_semantic_adapter=codex_semantic_adapter,
        raw_provider_probe=raw_provider_probe,
        request_kind_policy=request_kind_policy,
        repair_policy=repair_policy,
        vision=vision,
        tool_exposure=tool_exposure,
        codex_compatibility_policy=codex_compatibility_policy,
        collaboration_backend=collaboration_backend,
        execution_owner=ExecutionOwner.CODEX_CLIENT,
        named_mutations=frozenset(named_mutations),
        official_http_passthrough=official_http_passthrough,
        transparent_metered=transparent_metered,
        transparent_same_format=transparent_same_format,
        transparent_lightweight_fallback=transparent_lightweight_fallback,
        transparent_tool_loop_guard=(
            transparent_same_format and upstream_name != "official"
        ),
    )


def _route_plan_event_fields(plan: RoutePlan) -> dict[str, Any]:
    primary_attempt = plan.primary_attempt
    return {
        "route_plan_schema_version": plan.schema_version,
        "route_plan_summary_scope": "planned",
        "route_provider_id": plan.provider_id,
        "route_model_requested": plan.model_requested,
        "route_model_canonical": plan.canonical_model,
        "route_upstream_model": plan.upstream_model,
        "route_endpoint_url": (
            _safe_route_endpoint_url(primary_attempt.endpoint_url)
            if primary_attempt is not None
            else None
        ),
        "configured_upstream_protocol_name": plan.configured_upstream_protocol_name,
        "configured_upstream_protocol": plan.configured_upstream_protocol.value,
        "protocol_capability_state": plan.protocol_capability_state.value,
        "protocol_failure_reason": plan.protocol_failure_reason,
        "route_attempts": [
            attempt.telemetry_snapshot() for attempt in plan.attempts
        ],
        "wire_format_adapter": (
            primary_attempt.wire_format_adapter
            if primary_attempt is not None
            else plan.wire_format_adapter
        ),
        "route_plan_primary_wire_format_adapter": (
            primary_attempt.wire_format_adapter
            if primary_attempt is not None
            else plan.wire_format_adapter
        ),
        "caller_request_body_mode": plan.caller_request_body_mode.value,
        "prepared_request_protocol": plan.prepared_request_protocol.value,
        "codex_semantic_adapter": plan.codex_semantic_adapter,
        "request_kind": plan.request_kind,
        "raw_provider_probe": plan.raw_provider_probe,
        "request_kind_policy": plan.request_kind_policy,
        "retry_policy": (
            primary_attempt.retry.policy.value
            if primary_attempt is not None
            else plan.retry_policy.value
        ),
        "retry_eligibility": (
            primary_attempt.retry.eligibility.value
            if primary_attempt is not None
            else plan.retry_eligibility.value
        ),
        "usage_policy": (
            primary_attempt.usage_policy.value
            if primary_attempt is not None
            else plan.usage_policy.value
        ),
        "repair_policy": plan.repair_policy,
        "capability_manifest_version": plan.capability_manifest_version,
        "capability_manifest_hash": plan.capability_manifest_hash,
        "capability_manifest_state": plan.capability_manifest_state.value,
        "capability_binding_source_present": (
            plan.capability_binding.source_present
        ),
        "capability_binding_schema_version": (
            plan.capability_binding.schema_version
        ),
        "capability_binding_provider_id": (
            plan.capability_binding.provider_id
        ),
        "capability_binding_model_id": plan.capability_binding.model_id,
        "capability_binding_configured_upstream_protocol": (
            plan.capability_binding.configured_upstream_protocol.value
        ),
        "capability_binding_upstream_protocol": (
            plan.capability_binding.binding_upstream_protocol.value
        ),
        "capability_binding_route_scope_state": (
            plan.capability_binding.route_scope_state.value
        ),
        "capability_binding_route_scope_failure_reason": (
            plan.capability_binding.route_scope_failure_reason
        ),
        "authentication_strategy": (
            primary_attempt.authentication_strategy.value
            if primary_attempt is not None
            else plan.authentication_strategy.value
        ),
        "tool_requested_exposure_mode": plan.tool_exposure.requested_mode.value,
        "tool_exposure_mode": plan.tool_exposure.effective_mode.value,
        "tool_capability_state": plan.tool_exposure.capability_state.value,
        "gateway_schema_injection": plan.tool_exposure.gateway_schema_injection,
        "strip_caller_tools": plan.tool_exposure.strip_caller_tools,
        "codex_compatibility_policy": plan.codex_compatibility_policy.value,
        "collaboration_backend": plan.collaboration_backend.value,
        "execution_owner": plan.execution_owner.value,
        "streaming_policy": (
            primary_attempt.streaming_policy.value
            if primary_attempt is not None
            else plan.streaming_policy.value
        ),
        "transport_policy": (
            primary_attempt.transport_policy.value
            if primary_attempt is not None
            else plan.transport_policy.value
        ),
        "request_mutation_policy": (
            primary_attempt.request_mutation_policy.value
            if primary_attempt is not None
            else plan.request_mutation_policy.value
        ),
        "response_mutation_policy": (
            primary_attempt.response_mutation_policy.value
            if primary_attempt is not None
            else plan.response_mutation_policy.value
        ),
        "sse_mutation_policy": (
            primary_attempt.sse_mutation_policy.value
            if primary_attempt is not None
            else plan.sse_mutation_policy.value
        ),
        "vision_proxy_policy": plan.vision.policy,
        "vision_action": plan.vision.action.value,
        "vision_network_action": plan.vision.network_action.value,
        "vision_input_has_image": plan.vision.input_has_image,
        "vision_target_accepts_images": plan.vision.target_accepts_images,
        "transparent_tool_loop_guard": plan.transparent_tool_loop_guard,
        "mutation_summary_scope": "planned_union",
        "planned_mutation_summary": [
            mutation.value for mutation in plan.mutation_summary
        ],
        "mutation_summary": [
            mutation.value for mutation in plan.mutation_summary
        ],
    }


def _route_attempt_event_fields(
    attempt: RouteAttemptPlan,
    *,
    provider_id: str | None = None,
    model_requested: str | None = None,
    model_canonical: str | None = None,
    upstream_model: str | None = None,
) -> dict[str, Any]:
    snapshot = attempt.telemetry_snapshot()
    retry = snapshot["retry"]
    return {
        "execution_summary_scope": "selected_attempt_plan",
        "route_attempt_provider_id": provider_id,
        "route_attempt_model_requested": model_requested,
        "route_attempt_model_canonical": model_canonical,
        "route_attempt_upstream_model": upstream_model,
        "route_attempt_endpoint_url": snapshot["endpoint_url"],
        "executed_upstream_protocol": snapshot["upstream_protocol"],
        "executed_wire_format_adapter": snapshot["wire_format_adapter"],
        "executed_request_conversion_steps": snapshot[
            "request_conversion_steps"
        ],
        "executed_attempt_mutation_summary": snapshot[
            "mutation_summary"
        ],
        "route_attempt_index": snapshot["index"],
        "route_attempt_protocol": snapshot["upstream_protocol"],
        "route_attempt_wire_format_adapter": snapshot[
            "wire_format_adapter"
        ],
        "route_attempt_request_conversion_steps": snapshot[
            "request_conversion_steps"
        ],
        "route_attempt_request_body_mode": snapshot["request_body_mode"],
        "route_attempt_mutation_summary_scope": "attempt_plan",
        "route_attempt_authentication_strategy": snapshot[
            "authentication_strategy"
        ],
        "route_attempt_streaming_policy": snapshot["streaming_policy"],
        "route_attempt_usage_policy": snapshot["usage_policy"],
        "route_attempt_transport_policy": snapshot["transport_policy"],
        "route_attempt_retry_policy": retry["policy"],
        "route_attempt_retry_eligibility": retry["eligibility"],
        "route_attempt_base_open_attempts": retry["base_open_attempts"],
        "route_attempt_base_relay_attempts": retry[
            "base_relay_attempts"
        ],
        "route_attempt_open_attempt_budget": retry[
            "open_attempt_budget"
        ],
        "route_attempt_request_timeout_seconds": retry[
            "request_timeout_seconds"
        ],
        "route_attempt_request_mutation_policy": snapshot[
            "request_mutation_policy"
        ],
        "route_attempt_response_mutation_policy": snapshot[
            "response_mutation_policy"
        ],
        "route_attempt_sse_mutation_policy": snapshot[
            "sse_mutation_policy"
        ],
        "route_attempt_verify_cross_protocol_source": snapshot[
            "verify_cross_protocol_source"
        ],
        "route_attempt_tool_protocol": snapshot["tool_protocol"],
        "route_attempt_tool_surface_strategy": snapshot[
            "tool_surface_strategy"
        ],
        "route_attempt_native_responses_tool_codec": snapshot[
            "native_responses_tool_codec"
        ],
        "route_attempt_fallback_http_statuses": snapshot[
            "fallback_http_statuses"
        ],
        "route_attempt_mutation_summary": snapshot["mutation_summary"],
    }


def _request_observability_with_prefix(fields: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    renamed: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "request_body_hmac":
            renamed[f"{prefix}_request_body_hmac"] = value
        elif key == "request_body_hmac_skipped":
            renamed[f"{prefix}_request_body_hmac_skipped"] = value
        elif key == "request_prefix_hmac":
            renamed[f"{prefix}_request_prefix_hmac"] = value
        elif key == "prefix_bytes":
            renamed[f"{prefix}_prefix_bytes"] = value
        elif key == "prompt_cache_key_hash":
            renamed[f"{prefix}_prompt_cache_key_hash"] = value
    return renamed


def _is_event_stream(headers: Mapping[str, str] | Any) -> bool:
    content_type = _get_header(headers, "Content-Type")
    if content_type and "text/event-stream" in content_type.lower():
        return True
    # Some upstreams (e.g. chatgpt.com/backend-api/codex) return SSE without
    # an explicit Content-Type header but do signal chunked transfer.
    transfer_encoding = _get_header(headers, "Transfer-Encoding")
    return bool(transfer_encoding and "chunked" in transfer_encoding.lower())


_UNSET_CONTENT_ENCODING = object()


def _filtered_response_headers(
    headers: Mapping[str, str] | Any,
    is_event_stream: bool,
    content_length: int | None = None,
    content_type: str | None = None,
    content_encoding: str | None | object = _UNSET_CONTENT_ENCODING,
) -> list[tuple[str, str]]:
    outgoing: list[tuple[str, str]] = []
    for key, value in _header_items(headers):
        lowered = key.lower()
        if lowered in HOP_BY_HOP_RESPONSE_HEADERS:
            continue
        if lowered == "content-length" and (is_event_stream or content_length is not None):
            continue
        if lowered == "content-type" and content_type is not None:
            continue
        if lowered == "content-encoding" and content_encoding is not _UNSET_CONTENT_ENCODING:
            continue
        outgoing.append((key, value))
    if content_type is not None:
        outgoing.append(("Content-Type", content_type))
    if content_length is not None:
        outgoing.append(("Content-Length", str(content_length)))
    if content_encoding is not _UNSET_CONTENT_ENCODING and isinstance(content_encoding, str) and content_encoding:
        outgoing.append(("Content-Encoding", content_encoding))
    return outgoing


def materialize_operational_authentication(
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
) -> OperationalAuthentication:
    strategy = _authentication_strategy(upstream.get("auth") or "unknown")
    if strategy == AuthenticationStrategy.INCOMING:
        return OperationalAuthentication(
            strategy,
            authorization=_get_header(incoming_headers, "Authorization"),
        )
    if strategy == AuthenticationStrategy.OLLAMA_API_KEY:
        api_key = os.environ.get("OLLAMA_API_KEY")
        return OperationalAuthentication(
            strategy,
            authorization=f"Bearer {api_key}" if api_key else None,
        )
    if strategy == AuthenticationStrategy.API_KEY:
        api_key = upstream.get("api_key")
        return OperationalAuthentication(
            strategy,
            authorization=f"Bearer {api_key}" if api_key else None,
        )
    if strategy == AuthenticationStrategy.CODEX_AUTH:
        return OperationalAuthentication(
            strategy,
            authorization=f"Bearer {codex_access_token()}",
            account_id=codex_account_id(),
            generated_session_id=(
                _get_header(incoming_headers, "Session-id")
                or str(uuid.uuid4())
            ),
            generated_client_request_id=(
                _get_header(incoming_headers, "X-client-request-id")
                or str(uuid.uuid4())
            ),
        )
    return OperationalAuthentication(strategy, authorization=None)


def upstream_headers(
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
    drop_content_encoding: bool = False,
    behavior_profile: str | None = None,
    model_id: str | None = None,
    authentication_strategy: AuthenticationStrategy | None = None,
    request_mutation_policy: MutationPolicy | None = None,
    operational_authentication: OperationalAuthentication | None = None,
) -> dict[str, str]:
    auth_mode = (
        operational_authentication.strategy.value
        if operational_authentication is not None
        else authentication_strategy.value
        if authentication_strategy is not None
        else upstream.get("auth")
    )
    outgoing: dict[str, str] = {}
    upstream_model_id = canonical_model_id(
        str(upstream.get("upstream_model") or model_id or "")
    ).lower()
    if upstream_model_id.startswith(OFFICIAL_ALIAS_PREFIX):
        upstream_model_id = upstream_model_id[len(OFFICIAL_ALIAS_PREFIX) :]
    drop_responses_lite_header = (
        auth_mode == "codex_auth" and upstream_model_id in OFFICIAL_RESPONSES_LITE_UNSUPPORTED_MODELS
    )

    for key, value in _header_items(incoming_headers):
        lowered = key.lower()
        if lowered in HOP_BY_HOP_REQUEST_HEADERS or lowered == "authorization":
            continue
        if drop_responses_lite_header and lowered == "x-openai-internal-codex-responses-lite":
            continue
        if drop_content_encoding and lowered == "content-encoding":
            continue
        outgoing[key] = value

    if auth_mode == "incoming":
        incoming_auth = (
            operational_authentication.authorization
            if operational_authentication is not None
            else _get_header(incoming_headers, "Authorization")
        )
        if incoming_auth:
            outgoing["Authorization"] = incoming_auth
    elif auth_mode == "ollama_api_key":
        if operational_authentication is not None:
            authorization = operational_authentication.authorization
            if authorization is None:
                raise ValueError("OLLAMA_API_KEY is not set")
        else:
            api_key = os.environ.get("OLLAMA_API_KEY")
            if not api_key:
                raise ValueError("OLLAMA_API_KEY is not set")
            authorization = f"Bearer {api_key}"
        outgoing["Authorization"] = authorization
    elif auth_mode == "api_key":
        if operational_authentication is not None:
            authorization = operational_authentication.authorization
            if authorization is None:
                raise ValueError(
                    "API key is not set for upstream: "
                    f"{upstream.get('name', 'unknown')}"
                )
        else:
            api_key = upstream.get("api_key")
            if not api_key:
                raise ValueError(
                    "API key is not set for upstream: "
                    f"{upstream.get('name', 'unknown')}"
                )
            authorization = f"Bearer {api_key}"
        outgoing["Authorization"] = authorization
    elif auth_mode == "codex_auth":
        strict_official_passthrough = (
            request_mutation_policy
            == MutationPolicy.OFFICIAL_PASSTHROUGH
            if request_mutation_policy is not None
            else behavior_profile
            == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
        )
        authorization = (
            operational_authentication.authorization
            if operational_authentication is not None
            else f"Bearer {codex_access_token()}"
        )
        outgoing["Authorization"] = authorization
        # The chatgpt.com backend requires the account id header to identify
        # the subscription. Inject it from auth.json when not already present.
        if not _get_header(outgoing, "Chatgpt-account-id"):
            account = (
                operational_authentication.account_id
                if operational_authentication is not None
                else codex_account_id()
            )
            if account:
                outgoing["Chatgpt-account-id"] = account
        if not strict_official_passthrough:
            # The chatgpt.com/backend-api/codex endpoint expects Codex CLI-style
            # headers. When the caller (e.g. ZCode) does not provide them, inject
            # sensible defaults so the backend does not reject the request.
            if not _get_header(outgoing, "Accept"):
                outgoing["Accept"] = "text/event-stream"
            if not _get_header(outgoing, "Originator"):
                outgoing["Originator"] = "codexhub-proxy"
            if not _get_header(outgoing, "User-Agent"):
                outgoing["User-Agent"] = "Codex Desktop/0.142.4 (CodexHub proxy)"
            # The backend requires session/thread identifiers. Generate per-request
            # UUIDs when the caller doesn't supply them.
            session_id = _get_header(outgoing, "Session-id")
            if not session_id:
                session_id = (
                    operational_authentication.generated_session_id
                    if operational_authentication is not None
                    else str(uuid.uuid4())
                )
                if not session_id:
                    raise ValueError(
                        "materialized Codex auth is missing session identity"
                    )
                outgoing["Session-id"] = session_id
            if not _get_header(outgoing, "Thread-id"):
                outgoing["Thread-id"] = session_id
            if not _get_header(outgoing, "X-codex-window-id"):
                outgoing["X-codex-window-id"] = f"{session_id}:1"
            if not _get_header(outgoing, "X-client-request-id"):
                client_request_id = (
                    operational_authentication.generated_client_request_id
                    if operational_authentication is not None
                    else str(uuid.uuid4())
                )
                if not client_request_id:
                    raise ValueError(
                        "materialized Codex auth is missing request identity"
                    )
                outgoing["X-client-request-id"] = client_request_id
    else:
        raise ValueError(f"unsupported upstream auth mode: {auth_mode}")

    return outgoing


def bind_route_plan_operational_authentication(
    plan: RoutePlan,
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
    operational_authentication: OperationalAuthentication,
    *,
    drop_content_encoding: bool = False,
) -> RoutePlan:
    """Return a new plan whose attempts freeze one request-scoped auth snapshot."""

    if not plan.attempts:
        raise ValueError(
            "cannot materialize authentication for a route plan without attempts"
        )
    primary_attempt = plan.attempts[0]
    if any(
        attempt.request_headers.materialized
        for attempt in plan.attempts
    ):
        raise ValueError("route attempt authentication was already materialized")
    if any(
        attempt.authentication_strategy
        != operational_authentication.strategy
        or attempt.request_mutation_policy
        != primary_attempt.request_mutation_policy
        for attempt in plan.attempts
    ):
        raise ValueError(
            "route attempts do not share one authentication/header policy"
        )
    request_headers = FrozenRequestHeaders(
        upstream_headers(
            incoming_headers,
            upstream,
            drop_content_encoding=drop_content_encoding,
            model_id=plan.canonical_model or plan.model_requested,
            authentication_strategy=primary_attempt.authentication_strategy,
            request_mutation_policy=primary_attempt.request_mutation_policy,
            operational_authentication=operational_authentication,
        ),
        materialized=True,
    )
    return replace(
        plan,
        attempts=tuple(
            replace(attempt, request_headers=request_headers)
            for attempt in plan.attempts
        ),
    )


def current_catalog_data() -> dict[str, Any]:
    """Read the atomically published catalog without launching discovery.

    Tauri owns Official acquisition and catalog refresh.  The Gateway only
    consumes the published snapshot at request handling time.
    """
    catalog_path = existing_generated_catalog_path()
    if not catalog_path.exists():
        return {"models": []}
    published_budgets = published_official_context_budgets(catalog_path)
    return catalog_with_vision_proxy_capabilities(
        catalog_with_openai_context_guard(
            catalog_with_official_fast_variants(json.loads(catalog_path.read_text(encoding="utf-8-sig"))),
            published_budgets,
            require_published_snapshot=True,
        )
    )


def published_official_context_budgets(catalog_path: Path) -> dict[str, Mapping[str, Any]]:
    """Read the Rust publication fence that commits an Official budget.

    The catalog and Codex runtime overlay are separate atomic files.  The
    refresh coordinator clears this fence before it begins an update, then
    commits it only after both files agree.  A missing, interrupted, or
    mismatched fence is therefore not a safe current Official snapshot.
    """

    state_path = catalog_path.parent / OFFICIAL_REFRESH_STATE_FILENAME
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping) or payload.get("publication_ready") is not True:
        return {}
    budgets = payload.get("published_context_budgets")
    if not isinstance(budgets, Mapping):
        return {}
    return {
        str(model_id): budget
        for model_id, budget in budgets.items()
        if isinstance(model_id, str) and isinstance(budget, Mapping)
    }


def catalog_with_openai_context_guard(
    catalog: dict[str, Any],
    published_budgets: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    require_published_snapshot: bool = False,
) -> dict[str, Any]:
    # The Direct Official budget is a safety invariant, not an optional
    # presentation preference.  Gateway request handling only projects the
    # atomically published resolver result and never refreshes it itself.
    models = catalog.get("models")
    if not isinstance(models, list):
        return catalog

    def positive_int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    def guarded_model(model: Any) -> Any:
        def without_context_budget() -> dict[str, Any]:
            updated = dict(model)
            updated.pop("context_window", None)
            updated.pop("max_context_window", None)
            updated.pop("effective_context_window_percent", None)
            return updated

        if not isinstance(model, Mapping):
            return model
        metadata = model.get("codex_proxy_metadata")
        if not isinstance(metadata, Mapping):
            return model
        if metadata.get("provider") != "openai" or metadata.get("upstream_name") != "official":
            return model
        budget = metadata.get("official_context_budget")
        if not isinstance(budget, Mapping):
            return without_context_budget()
        source = budget.get("source")
        freshness = budget.get("freshness")
        trusted_budget = (
            source == CURRENT_DIRECT_OFFICIAL_SOURCE and freshness == "fresh"
        ) or source == DEGRADED_LAST_KNOWN_OFFICIAL_SOURCE
        if not trusted_budget:
            return without_context_budget()
        guard_window = positive_int(budget.get("model_context_window"))
        if guard_window is None:
            guard_window = positive_int(budget.get("context_window"))
        effective_percent = positive_int(budget.get("effective_context_window_percent"))
        effective_window = positive_int(budget.get("effective_context_window"))
        auto_compact_limit = positive_int(budget.get("model_auto_compact_token_limit"))
        if (
            guard_window is None
            or effective_percent is None
            or effective_percent > 100
            or effective_window is None
            or effective_window > guard_window
            or auto_compact_limit is None
            or auto_compact_limit > effective_window
        ):
            # An incomplete or interrupted resolved snapshot must not keep a
            # larger stale catalog value visible to an Official caller.
            return without_context_budget()
        if require_published_snapshot:
            if not isinstance(published_budgets, Mapping):
                return without_context_budget()
            model_id = str(model.get("slug", "")).removeprefix("openai/")
            upstream_model = metadata.get("upstream_model")
            expected = published_budgets.get(model_id)
            if expected is None and isinstance(upstream_model, str):
                expected = published_budgets.get(upstream_model.removeprefix("openai/"))
            if not isinstance(expected, Mapping) or any(
                expected.get(key) != budget.get(key)
                for key in (
                    "model_context_window",
                    "effective_context_window_percent",
                    "effective_context_window",
                    "model_auto_compact_token_limit",
                )
            ):
                return without_context_budget()
        reported_windows = [
            value
            for value in (
                positive_int(model.get("context_window")),
                positive_int(model.get("max_context_window")),
            )
            if value is not None
        ]
        guarded_window = min(guard_window, *reported_windows) if reported_windows else guard_window
        return {
            **model,
            "context_window": guarded_window,
            "max_context_window": guarded_window,
            "effective_context_window_percent": effective_percent,
        }

    updated = dict(catalog)
    updated["models"] = [guarded_model(model) for model in models]
    return updated


def catalog_with_vision_proxy_capabilities(catalog: dict[str, Any]) -> dict[str, Any]:
    if not gateway_image_proxy_enabled():
        return catalog

    models = catalog.get("models")
    if not isinstance(models, list):
        return catalog

    updated = dict(catalog)
    updated["models"] = [
        {
            **model,
            "input_modalities": list(
                dict.fromkeys([*(model.get("input_modalities") or ["text"]), "image"])
            ),
        }
        if isinstance(model, Mapping)
        else model
        for model in models
    ]
    return updated


def catalog_with_official_fast_variants(catalog: dict[str, Any]) -> dict[str, Any]:
    models = catalog.get("models")
    if not isinstance(models, list):
        return catalog

    policy = load_policy(POLICY_PATH)
    models = canonical_catalog_models(models, policy)
    catalog["models"] = models

    by_slug = {
        canonical_model_id(str(model.get("slug", ""))): model
        for model in models
        if isinstance(model, Mapping)
    }
    for fast_model, upstream_model in OFFICIAL_FAST_VARIANT_BASE_MODELS.items():
        legacy_base_slug = f"{OFFICIAL_ALIAS_PREFIX}{upstream_model}"
        fast_slug = fast_model
        base_model = by_slug.get(upstream_model) or by_slug.get(legacy_base_slug)
        if not isinstance(base_model, Mapping) or fast_slug in by_slug:
            continue
        fast_entry = deepcopy(dict(base_model))
        fast_entry["slug"] = fast_slug
        fast_entry["display_name"] = OFFICIAL_FAST_VARIANT_DISPLAY_NAMES.get(
            fast_model,
            f"{base_model.get('display_name', upstream_model)} Fast",
        )
        metadata = dict(fast_entry.get("codex_proxy_metadata", {}))
        metadata.update(
            {
                "provider": "openai",
                "upstream_model": upstream_model,
                "service_tier": OFFICIAL_FAST_VARIANT_SERVICE_TIER,
            }
        )
        fast_entry["codex_proxy_metadata"] = metadata
        models.append(fast_entry)
        by_slug[fast_slug] = fast_entry
    return catalog


def canonical_catalog_models(
    models: list[Any],
    policy: CatalogPolicy,
) -> list[Any]:
    known_official_ids = catalog_known_official_model_ids()
    for model in models:
        if not isinstance(model, Mapping):
            continue
        slug = canonical_model_id(str(model.get("slug", "")))
        if slug.startswith("gpt-"):
            known_official_ids.add(slug)

    output: list[Any] = []
    official_positions: dict[str, int] = {}
    official_bare_sources: dict[str, bool] = {}
    for model in models:
        if not isinstance(model, Mapping):
            output.append(model)
            continue
        raw_slug = canonical_model_id(str(model.get("slug", "")))
        is_legacy_alias = raw_slug.startswith(OFFICIAL_ALIAS_PREFIX + "gpt-")
        if is_legacy_alias:
            canonical_slug = raw_slug[len(OFFICIAL_ALIAS_PREFIX) :]
            if canonical_slug not in known_official_ids:
                continue
        elif raw_slug.startswith("gpt-"):
            canonical_slug = raw_slug
        else:
            output.append(model)
            continue

        candidate = deepcopy(dict(model))
        candidate["slug"] = canonical_slug
        candidate["display_name"] = official_short_display_name(canonical_slug, candidate, policy)
        position = official_positions.get(canonical_slug)
        if position is None:
            official_positions[canonical_slug] = len(output)
            official_bare_sources[canonical_slug] = not is_legacy_alias
            output.append(candidate)
            continue

        existing = output[position]
        existing_is_bare = official_bare_sources.get(canonical_slug, False)
        fresh = candidate if not is_legacy_alias or not existing_is_bare else deepcopy(dict(existing))
        if "enabled" in existing or "enabled" in candidate:
            fresh["enabled"] = bool(existing.get("enabled", True) or candidate.get("enabled", True))
        output[position] = fresh
        official_bare_sources[canonical_slug] = existing_is_bare or not is_legacy_alias
    return output


def _json_response_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


RESPONSE_ENDPOINT_SUFFIXES = ("/responses", "/response")
KNOWN_UPSTREAM_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    *RESPONSE_ENDPOINT_SUFFIXES,
    "/messages",
    "/models",
)


def _upstream_endpoint_url(upstream: Mapping[str, Any], path: str) -> str:
    base = str(upstream["base_url"]).strip().rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    if _upstream_base_path_matches(base, path):
        return base
    root = _upstream_endpoint_root(base)
    if upstream.get("auth") == "codex_auth":
        return root + path
    if _upstream_base_has_version_suffix(root):
        return root + path
    return root + "/v1" + path


def _upstream_endpoint_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    lowered_path = urlsplit(base).path.rstrip("/").lower()
    for suffix in KNOWN_UPSTREAM_ENDPOINT_SUFFIXES:
        if lowered_path.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


def _upstream_base_path_matches(base_url: str, path: str) -> bool:
    lowered_path = urlsplit(base_url).path.rstrip("/").lower()
    requested_path = path.lower()
    if requested_path == "/responses":
        return any(lowered_path.endswith(suffix) for suffix in RESPONSE_ENDPOINT_SUFFIXES)
    return lowered_path.endswith(requested_path)


def _upstream_base_has_version_suffix(base_url: str) -> bool:
    path = urlsplit(base_url).path.rstrip("/")
    if not path:
        return False
    return bool(re.fullmatch(r"v\d+(?:\.\d+)?", path.rsplit("/", 1)[-1].lower()))


def _responses_url(upstream: Mapping[str, Any], request_path: str) -> str:
    parsed = urlsplit(request_path)
    path = parsed.path
    if path.startswith("/v1/"):
        path = path[3:]
    elif not path.startswith("/"):
        path = "/" + path
    url = _upstream_endpoint_url(upstream, path)
    if parsed.query:
        url += "?" + parsed.query
    return url


def _chat_completions_url(upstream: Mapping[str, Any]) -> str:
    return _upstream_endpoint_url(upstream, "/chat/completions")


def _modalities_include_image(value: Any) -> bool:
    if not isinstance(value, (list, tuple, set)):
        return False
    return any(str(item).lower() == "image" for item in value)


def _catalog_input_modalities(model_id: str | None, upstream: Mapping[str, Any] | None = None) -> Any:
    candidates: list[str] = []
    for value in (model_id, upstream.get("upstream_model") if upstream else None):
        if not isinstance(value, str) or not value.strip():
            continue
        slug = canonical_model_id(value)
        if not slug:
            continue
        candidates.append(slug)
        if slug.startswith(OFFICIAL_ALIAS_PREFIX):
            candidates.append(slug[len(OFFICIAL_ALIAS_PREFIX) :])
        else:
            candidates.append(f"{OFFICIAL_ALIAS_PREFIX}{slug}")

    catalog = generated_catalog_by_slug()
    for candidate in dict.fromkeys(candidates):
        model = catalog.get(_catalog_identity_slug(candidate))
        if isinstance(model, Mapping) and "input_modalities" in model:
            return model.get("input_modalities")
    return None


def model_supports_image(model_id: str | None, upstream: Mapping[str, Any] | None = None) -> bool:
    if upstream and _modalities_include_image(upstream.get("input_modalities")):
        return True
    return _modalities_include_image(_catalog_input_modalities(model_id, upstream))


def _is_image_part(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    part_type = value.get("type")
    if part_type == "input_image":
        return any(isinstance(value.get(key), str) and value.get(key) for key in ("image_url", "file_id"))
    if part_type == "image_url":
        image_url = value.get("image_url")
        return isinstance(image_url, Mapping) and isinstance(image_url.get("url"), str) and bool(image_url.get("url"))
    return False


def _value_contains_image(value: Any) -> bool:
    if _is_image_part(value):
        return True
    if isinstance(value, list):
        return any(_value_contains_image(item) for item in value)
    if isinstance(value, Mapping):
        return any(_value_contains_image(item) for item in value.values())
    return False


def _normalized_vision_image_part(part: Mapping[str, Any]) -> dict[str, Any]:
    if part.get("type") == "image_url" and isinstance(part.get("image_url"), Mapping):
        image_url = part["image_url"].get("url")
        output = {"type": "input_image", "image_url": image_url}
    else:
        output = {"type": "input_image"}
        for key in ("image_url", "file_id"):
            value = part.get(key)
            if isinstance(value, str) and value:
                output[key] = value
    detail = part.get("detail")
    if isinstance(detail, str) and detail:
        output["detail"] = detail
    return output


def _image_proxy_cache_key(part: Mapping[str, Any], vision_model: str) -> str:
    normalized = _normalized_vision_image_part(part)
    raw = json.dumps(
        {
            "image": normalized,
            "vision_model": vision_model,
            "prompt_version": IMAGE_PROXY_PROMPT_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _image_proxy_unique_image_count(value: Any, vision_model: str) -> int:
    cache_keys: set[str] = set()

    def collect(item: Any) -> None:
        if _is_image_part(item):
            cache_keys.add(_image_proxy_cache_key(item, vision_model))
            return
        if isinstance(item, list):
            for child in item:
                collect(child)
            return
        if isinstance(item, Mapping):
            for child in item.values():
                collect(child)

    collect(value)
    return len(cache_keys)


def _ensure_image_proxy_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_proxy_cache (
            cache_key TEXT PRIMARY KEY,
            vision_model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )


def _image_proxy_cache_lookup(cache_key: str) -> str | None:
    path = Path(IMAGE_PROXY_CACHE_PATH)
    try:
        with IMAGE_PROXY_CACHE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                _ensure_image_proxy_cache(conn)
                row = conn.execute(
                    "SELECT description FROM image_proxy_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        logger.warning("vision proxy cache lookup failed: %s", type(exc).__name__)
        return None
    if not row:
        return None
    description = row[0]
    return description if isinstance(description, str) and description else None


def _image_proxy_cache_store(cache_key: str, vision_model: str, description: str) -> None:
    path = Path(IMAGE_PROXY_CACHE_PATH)
    try:
        with IMAGE_PROXY_CACHE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                _ensure_image_proxy_cache(conn)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO image_proxy_cache
                    (cache_key, vision_model, prompt_version, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cache_key, vision_model, IMAGE_PROXY_PROMPT_VERSION, description, int(time.time())),
                )
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        logger.warning("vision proxy cache store failed: %s", type(exc).__name__)


def _extract_model_response_text(payload: Any) -> str:
    text_parts: list[str] = []
    if isinstance(payload, Mapping):
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, Mapping):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, Mapping) and part.get("type") in {"output_text", "text"}:
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            text_parts.append(text)
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, Mapping):
                    continue
                message = choice.get("message")
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, Mapping) and part.get("type") == "text":
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                text_parts.append(text)
    return "\n".join(part.strip() for part in text_parts if part.strip()).strip()


def _image_proxy_response_body(response: Any) -> bytes:
    if _is_event_stream(response.headers):
        events: list[Mapping[str, Any]] = []
        assembler = SseEventAssembler()
        while True:
            chunk = response.readline()
            if not chunk:
                break
            for frame in assembler.feed(chunk):
                payload = _converted_sse_payload(frame)
                if isinstance(payload, Mapping):
                    events.append(payload)
        termination = assembler.finish()
        if termination.disposition == "incomplete":
            raise UpstreamStreamIncompleteError(
                "Vision Proxy SSE stream ended with an incomplete pending frame"
            )
        for frame in termination.events:
            payload = _converted_sse_payload(frame)
            if isinstance(payload, Mapping):
                events.append(payload)
        return _events_to_responses_body(events)

    body = b""
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        body += chunk
    return body


def _call_vision_model_for_image_description(
    part: Mapping[str, Any],
    vision_model: str,
    vision_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> str:
    started_at = time.monotonic()
    upstream_format = str(vision_upstream.get("upstream_format") or "responses")
    payload = {
        "model": vision_model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": IMAGE_PROXY_PROMPT},
                    _normalized_vision_image_part(part),
                ],
            }
        ],
        "stream": upstream_format != "chat_completions",
    }
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    vision_context = dict(event_context or {})
    vision_context["image_proxy"] = True
    vision_context["vision_model"] = canonical_model_id(vision_model)
    try:
        body = compatible_request_body(
            body,
            vision_upstream,
            model_id=vision_model,
            event_context=vision_context,
            inject_codex_tools=False,
        )
        try:
            vision_payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            vision_payload = None
        if isinstance(vision_payload, dict) and _strip_tools_for_text_only_proxy_payload(
            vision_payload,
            event_context=vision_context,
            upstream_name=str(vision_upstream.get("name", "unknown")),
            event_name="image_proxy_vision_tools_stripped",
        ):
            body = json.dumps(vision_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        upstream_url = _responses_url(vision_upstream, "/v1/responses")
        if upstream_format == "chat_completions":
            body = _responses_request_to_chat_completion_body(body)
            upstream_url = _chat_completions_url(vision_upstream)
        headers = upstream_headers({"Content-Type": "application/json"}, vision_upstream)
    except ValueError as exc:
        raise ImageProxyError(f"Vision model request is invalid: {exc}") from exc

    request = Request(upstream_url, data=body, headers=headers, method="POST")
    vision_upstream_name = str(vision_upstream.get("name", "unknown"))
    _write_adapter_event(
        event_context,
        "image_proxy_vision_request_start",
        vision_model=canonical_model_id(vision_model),
        upstream=vision_upstream_name,
        upstream_format=upstream_format,
        stream=payload["stream"],
    )
    try:
        with _open_upstream_response(
            request,
            upstream_name=vision_upstream_name,
            upstream_format=upstream_format,
            timeout=upstream_timeout_seconds(),
            event_context=vision_context,
            request_kind=RETRY_REQUEST_IMAGE_PROXY_VISION,
            max_attempts=1,
        ) as response:
            response_status = getattr(response, "status", None)
            response_body = _image_proxy_response_body(response)
    except BaseException as exc:
        _write_adapter_event(
            event_context,
            "image_proxy_vision_request_error",
            vision_model=canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            error=type(exc).__name__,
            detail=safe_upstream_error_detail(exc),
        )
        raise

    try:
        response_payload = json.loads(response_body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _write_adapter_event(
            event_context,
            "image_proxy_vision_request_error",
            vision_model=canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            status=response_status if isinstance(response_status, int) else None,
            error=type(exc).__name__,
            detail="Vision model returned an invalid response",
        )
        raise ImageProxyError("Vision model returned an invalid response") from exc
    description = _extract_model_response_text(response_payload)
    if not description:
        _write_adapter_event(
            event_context,
            "image_proxy_vision_request_error",
            vision_model=canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            status=response_status if isinstance(response_status, int) else None,
            error="EmptyImageDescription",
            detail="Vision model returned no image description",
            **_normalize_usage_for_event(_usage_from_payload(response_payload)),
        )
        raise ImageProxyError("Vision model returned no image description")
    _write_adapter_event(
        event_context,
        "image_proxy_vision_request_complete",
        vision_model=canonical_model_id(vision_model),
        upstream=vision_upstream_name,
        upstream_format=upstream_format,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        status=response_status if isinstance(response_status, int) else None,
        description_length=len(description),
        **_normalize_usage_for_event(_usage_from_payload(response_payload)),
    )
    return description


def _image_proxy_description_for_part(
    part: Mapping[str, Any],
    vision_model: str,
    vision_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> str:
    cache_key = _image_proxy_cache_key(part, vision_model)
    cached = _image_proxy_cache_lookup(cache_key)
    if cached is not None:
        _write_adapter_event(event_context, "image_proxy_cache_hit", vision_model=canonical_model_id(vision_model))
        return cached
    description = _call_vision_model_for_image_description(part, vision_model, vision_upstream, event_context)
    _image_proxy_cache_store(cache_key, vision_model, description)
    return description


def _image_proxy_reference_for_part(part: Mapping[str, Any], vision_model: str) -> str:
    return f"codexhub://image/{_image_proxy_cache_key(part, vision_model)}"


def _image_description_part(description: str, image_path: str) -> dict[str, str]:
    safe_description = description.replace("</image>", "</ image>")
    return {
        "type": "input_text",
        "text": (
            "The Gateway has already read the user's attached image. "
            "Use the visual context below as the image content when answering. "
            "Do not mention the Gateway, preprocessing, replacement, missing images, "
            "or inability to view the original attachment. Answer directly.\n\n"
            f'Visual context:\n<image path="{image_path}">\n{safe_description}\n</image>'
        ),
    }


def _chat_image_description_part(description: str, image_path: str) -> dict[str, str]:
    return {
        "type": "text",
        "text": _image_description_part(description, image_path)["text"],
    }


def _replace_image_parts(value: Any, describe: Any) -> tuple[Any, bool]:
    if _is_image_part(value):
        description, image_path = describe(value)
        return _image_description_part(description, image_path), True
    if isinstance(value, list):
        changed = False
        output = []
        for item in value:
            replacement, item_changed = _replace_image_parts(item, describe)
            changed = changed or item_changed
            output.append(replacement)
        return output, changed
    if isinstance(value, dict):
        changed = False
        output = dict(value)
        for key, item in value.items():
            replacement, item_changed = _replace_image_parts(item, describe)
            if item_changed:
                output[key] = replacement
                changed = True
        return output, changed
    return value, False


def _replace_chat_image_parts(value: Any, describe: Any) -> tuple[Any, bool]:
    if _is_image_part(value):
        description, image_path = describe(value)
        return _chat_image_description_part(description, image_path), True
    if isinstance(value, list):
        changed = False
        output = []
        for item in value:
            replacement, item_changed = _replace_chat_image_parts(item, describe)
            changed = changed or item_changed
            output.append(replacement)
        return output, changed
    if isinstance(value, dict):
        changed = False
        output = dict(value)
        for key, item in value.items():
            replacement, item_changed = _replace_chat_image_parts(item, describe)
            if item_changed:
                output[key] = replacement
                changed = True
        return output, changed
    return value, False


def _vision_proxy_context(
    event_context: Mapping[str, Any] | None,
    vision_proxy_policy: str,
) -> dict[str, Any] | None:
    if event_context is None:
        return None
    context = dict(event_context)
    context["vision_proxy_policy"] = vision_proxy_policy
    return context


def _image_proxy_vision_upstream() -> tuple[str, Mapping[str, Any]]:
    vision_model = gateway_image_proxy_model()
    if not vision_model:
        raise ImageProxyError("Vision model is not configured for Vision Proxy")
    try:
        vision_upstream = choose_upstream(vision_model)
    except ValueError as exc:
        raise ImageProxyError(f"Vision model is not available: {vision_model}: {exc}") from exc
    if not model_supports_image(vision_model, vision_upstream):
        raise ImageProxyError(f"Vision model does not support image input: {vision_model}")
    return vision_model, vision_upstream


def apply_image_proxy_to_responses_payload(
    payload: dict[str, Any],
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
    *,
    image_proxy_enabled: bool | None = None,
    target_accepts_images: bool | None = None,
) -> bool:
    if image_proxy_enabled is None:
        image_proxy_enabled = gateway_image_proxy_enabled()
    if not image_proxy_enabled:
        return False
    if target_accepts_images is None:
        target_accepts_images = bool(
            target_model and model_supports_image(target_model, target_upstream)
        )
    if target_accepts_images:
        return False
    if not _value_contains_image(payload.get("input")):
        return False

    vision_model, vision_upstream = _image_proxy_vision_upstream()

    descriptions: dict[str, str] = {}
    progress_sent = False
    image_count = _image_proxy_unique_image_count(payload.get("input"), vision_model)

    def emit_progress_once() -> bool:
        nonlocal progress_sent
        if progress_sent or progress_callback is None:
            return True
        if not progress_callback(
            {
                "type": "image_proxy",
                "status": "reading",
                "image_count": image_count,
                "vision_model": canonical_model_id(vision_model),
            }
        ):
            return False
        progress_sent = True
        return True

    def describe(part: Mapping[str, Any]) -> tuple[str, str]:
        cache_key = _image_proxy_cache_key(part, vision_model)
        if cache_key not in descriptions:
            if _image_proxy_cache_lookup(cache_key) is None:
                if not emit_progress_once():
                    raise DownstreamClosedDuringImageProxyError("downstream closed during Vision Proxy")
            descriptions[cache_key] = _image_proxy_description_for_part(
                part,
                vision_model,
                vision_upstream,
                event_context=event_context,
            )
        return descriptions[cache_key], _image_proxy_reference_for_part(part, vision_model)

    replacement, changed = _replace_image_parts(payload.get("input"), describe)
    if changed:
        payload["input"] = replacement
        _write_adapter_event(
            event_context,
            "image_proxy_applied",
            vision_model=canonical_model_id(vision_model),
            target_model=canonical_model_id(target_model) if target_model else None,
            image_count=len(descriptions),
        )
    return changed


def apply_image_proxy_to_chat_payload(
    payload: dict[str, Any],
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
    *,
    image_proxy_enabled: bool | None = None,
    target_accepts_images: bool | None = None,
) -> bool:
    if image_proxy_enabled is None:
        image_proxy_enabled = gateway_image_proxy_enabled()
    if not image_proxy_enabled:
        return False
    if target_accepts_images is None:
        target_accepts_images = bool(
            target_model and model_supports_image(target_model, target_upstream)
        )
    if target_accepts_images:
        return False
    if not _value_contains_image(payload.get("messages")):
        return False

    vision_model, vision_upstream = _image_proxy_vision_upstream()
    descriptions: dict[str, str] = {}
    progress_sent = False
    image_count = _image_proxy_unique_image_count(payload.get("messages"), vision_model)

    def emit_progress_once() -> bool:
        nonlocal progress_sent
        if progress_sent or progress_callback is None:
            return True
        if not progress_callback(
            {
                "type": "image_proxy",
                "status": "reading",
                "image_count": image_count,
                "vision_model": canonical_model_id(vision_model),
            }
        ):
            return False
        progress_sent = True
        return True

    def describe(part: Mapping[str, Any]) -> tuple[str, str]:
        cache_key = _image_proxy_cache_key(part, vision_model)
        if cache_key not in descriptions:
            if _image_proxy_cache_lookup(cache_key) is None:
                if not emit_progress_once():
                    raise DownstreamClosedDuringImageProxyError("downstream closed during Vision Proxy")
            descriptions[cache_key] = _image_proxy_description_for_part(
                part,
                vision_model,
                vision_upstream,
                event_context=event_context,
            )
        return descriptions[cache_key], _image_proxy_reference_for_part(part, vision_model)

    replacement, changed = _replace_chat_image_parts(payload.get("messages"), describe)
    if changed:
        payload["messages"] = replacement
        _write_adapter_event(
            event_context,
            "image_proxy_applied",
            vision_model=canonical_model_id(vision_model),
            target_model=canonical_model_id(target_model) if target_model else None,
            image_count=len(descriptions),
        )
    return changed


def apply_vision_proxy_adapter(
    payload: dict[str, Any],
    *,
    inbound_format: str,
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    vision_proxy_policy: str,
    image_proxy_enabled: bool | None = None,
    target_accepts_images: bool | None = None,
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
) -> bool:
    if vision_proxy_policy == VISION_PROXY_DISABLED:
        return False
    proxy_context = _vision_proxy_context(event_context, vision_proxy_policy)
    if inbound_format == "chat_completions":
        return apply_image_proxy_to_chat_payload(
            payload,
            target_model,
            target_upstream,
            event_context=proxy_context,
            progress_callback=progress_callback,
            image_proxy_enabled=image_proxy_enabled,
            target_accepts_images=target_accepts_images,
        )
    return apply_image_proxy_to_responses_payload(
        payload,
        target_model,
        target_upstream,
        event_context=proxy_context,
        progress_callback=progress_callback,
        image_proxy_enabled=image_proxy_enabled,
        target_accepts_images=target_accepts_images,
    )


def enforce_text_only_image_boundary(
    payload: dict[str, Any],
    *,
    inbound_format: str,
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    vision_plan: VisionPlan,
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
) -> bool:
    if vision_plan.action == VisionAction.PASS_THROUGH:
        return False
    if vision_plan.action == VisionAction.REJECT:
        model_label = (
            canonical_model_id(target_model)
            if target_model
            else "the target model"
        )
        raise ImageProxyError(
            f"{model_label} does not support image input and Vision Proxy is disabled."
        )
    if vision_plan.network_action != VisionNetworkAction.IMAGE_PROXY:
        raise ImageProxyError(
            "The planned Vision action has no executable network action."
        )

    changed = apply_vision_proxy_adapter(
        payload,
        inbound_format=inbound_format,
        target_model=target_model,
        target_upstream=target_upstream,
        vision_proxy_policy=vision_plan.policy,
        image_proxy_enabled=vision_plan.image_proxy_enabled,
        target_accepts_images=vision_plan.target_accepts_images,
        event_context=event_context,
        progress_callback=progress_callback,
    )
    image_root = (
        payload.get("messages")
        if inbound_format == RouteProtocol.CHAT_COMPLETIONS.value
        else payload.get("input")
    )
    if _value_contains_image(image_root):
        raise ImageProxyError(
            "Vision Proxy could not replace the image for the text-only target model."
        )
    if changed:
        _write_adapter_event(
            event_context,
            "image_proxy_boundary_guard_applied",
            target_model=(
                canonical_model_id(target_model)
                if target_model
                else None
            ),
            inbound_format=inbound_format,
        )
    return changed


def _upstream_retry_status(exc: BaseException) -> int | None:
    status = getattr(exc, "code", None)
    return status if isinstance(status, int) else None


def _request_kind_retry_env_name(request_kind: str) -> str | None:
    if request_kind == RETRY_REQUEST_COMPACT:
        return "CODEX_PROXY_COMPACT_RETRY_MAX_ATTEMPTS"
    if request_kind == RETRY_REQUEST_MAIN_GENERATION:
        return "CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS"
    return None


def _request_kind_retry_settings_name(request_kind: str) -> str | None:
    if request_kind == RETRY_REQUEST_COMPACT:
        return "gateway_compact_retry_max_attempts"
    if request_kind == RETRY_REQUEST_MAIN_GENERATION:
        return "gateway_main_generation_retry_max_attempts"
    return None


def _default_retry_attempts_for_request_kind(request_kind: str) -> int:
    if request_kind == RETRY_REQUEST_COMPACT:
        return 3
    if request_kind == RETRY_REQUEST_IMAGE_PROXY_VISION:
        return 3
    if request_kind == RETRY_REQUEST_OFFICIAL_CONTROL:
        return 1
    return 5


def _bounded_retry_attempts(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(1, min(value, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return default
        return max(1, min(parsed, DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS))
    return default


def _upstream_retry_attempts(request_kind: str = RETRY_REQUEST_MAIN_GENERATION) -> int:
    if not gateway_auto_retry_enabled():
        return 1
    default = _default_retry_attempts_for_request_kind(request_kind)
    settings_name = _request_kind_retry_settings_name(request_kind)
    if settings_name:
        settings_value = _runtime_settings_value(settings_name)
        if settings_value is not None:
            return _bounded_retry_attempts(settings_value, default)
    env_name = _request_kind_retry_env_name(request_kind)
    if env_name:
        raw_value = os.environ.get(env_name)
        if raw_value is not None:
            return _bounded_retry_attempts(raw_value, default)
    return min(gateway_auto_retry_max_attempts(), default)


def _request_kind_retry_attempts_configured(request_kind: str) -> bool:
    settings_name = _request_kind_retry_settings_name(request_kind)
    if settings_name and _runtime_settings_value(settings_name) is not None:
        return True
    env_name = _request_kind_retry_env_name(request_kind)
    return bool(env_name and os.environ.get(env_name) is not None)


def _retry_attempts_for_failure_class(
    *,
    request_kind: str,
    base_attempts: int,
    failure_class: str,
    explicit_max_attempts: bool,
    stream_failure: bool = False,
) -> int:
    if (
        explicit_max_attempts
        or base_attempts <= 1
        or _request_kind_retry_attempts_configured(request_kind)
    ):
        return base_attempts
    if failure_class in CAPACITY_RETRY_FAILURE_CLASSES:
        return max(base_attempts, gateway_auto_retry_max_attempts())
    if stream_failure and failure_class == RETRY_FAILURE_QUICK_TRANSIENT:
        return max(base_attempts, gateway_auto_retry_max_attempts())
    return base_attempts


def _http_retry_header_override(exc: HTTPError) -> bool | None:
    value = _get_header(getattr(exc, "headers", {}), "x-should-retry")
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return None


def _http_error_body_bytes(exc: HTTPError) -> bytes:
    cached = getattr(exc, "_codexhub_error_body", None)
    if isinstance(cached, bytes):
        return cached
    fp = getattr(exc, "fp", None)
    if fp is None:
        return b""
    try:
        body = fp.read()
    except OSError:
        body = b""
    finally:
        try:
            fp.close()
        except OSError:
            pass
    replacement = io.BytesIO(body)
    exc.fp = replacement
    exc.file = replacement
    setattr(exc, "_codexhub_error_body", body)
    return body


def _http_error_payload(exc: HTTPError) -> Mapping[str, Any] | None:
    body = _http_error_body_bytes(exc)
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _payload_error_values(payload: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(payload, Mapping):
        return set()
    error = payload.get("error")
    values: set[str] = set()
    value_keys = (
        "__type",
        "code",
        "detail",
        "error_code",
        "error_type",
        "errorCode",
        "errorType",
        "message",
        "param",
        "reason",
        "status",
        "type",
    )

    def add_value(value: Any) -> None:
        if isinstance(value, str) and value:
            values.add(value.strip().lower())
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            values.add(str(value))

    def add_mapping_values(mapping: Mapping[str, Any]) -> None:
        for key in value_keys:
            add_value(mapping.get(key))
        nested_errors = mapping.get("errors")
        if isinstance(nested_errors, list):
            for item in nested_errors:
                if isinstance(item, Mapping):
                    add_mapping_values(item)
        nested_response = mapping.get("response")
        if isinstance(nested_response, Mapping):
            nested_error = nested_response.get("error")
            if isinstance(nested_error, Mapping):
                add_mapping_values(nested_error)

    if isinstance(error, Mapping):
        add_mapping_values(error)
    elif isinstance(error, str) and error:
        values.add(error.strip().lower())
    add_mapping_values(payload)
    return values


def _http_error_values(exc: HTTPError) -> set[str]:
    return _payload_error_values(_http_error_payload(exc))


def _http_error_values_contain(values: set[str], needles: tuple[str, ...]) -> bool:
    return any(needle in value for value in values for needle in needles)


def _failure_class_from_error_values(values: set[str]) -> str | None:
    if not values:
        return None
    if any(value in PERMANENT_UPSTREAM_ERROR_VALUES for value in values):
        return RETRY_FAILURE_PERMANENT
    if _http_error_values_contain(values, PERMANENT_UPSTREAM_ERROR_NEEDLES):
        return RETRY_FAILURE_PERMANENT
    if any(value in PROVIDER_THROTTLE_ERROR_VALUES for value in values) or _http_error_values_contain(
        values,
        PROVIDER_THROTTLE_ERROR_NEEDLES,
    ):
        return RETRY_FAILURE_PROVIDER_THROTTLE
    if any(value in PROVIDER_OVERLOADED_ERROR_VALUES for value in values) or _http_error_values_contain(
        values,
        PROVIDER_OVERLOADED_ERROR_NEEDLES,
    ):
        return RETRY_FAILURE_PROVIDER_OVERLOADED
    if _http_error_values_contain(values, PERMANENT_UPSTREAM_AUTH_NEEDLES):
        return RETRY_FAILURE_PERMANENT
    return None


def _status_allows_capacity_error_value(status: int | None) -> bool:
    if status is None:
        return True
    if status == 400:
        return True
    return status not in PERMANENT_HTTP_ERROR_STATUSES


def _retry_after_delay_seconds(exc: BaseException | None) -> int | None:
    if not isinstance(exc, HTTPError):
        return None
    value = _get_header(getattr(exc, "headers", {}), "retry-after")
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    try:
        seconds = float(stripped)
    except ValueError:
        seconds = None
    if seconds is not None:
        return max(0, math.ceil(seconds))
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0, math.ceil(retry_at.timestamp() - time.time()))


def _upstream_failure_class(exc: BaseException) -> str:
    if isinstance(exc, UpstreamStreamInterruptedError):
        return _upstream_failure_class(exc.cause)
    if isinstance(exc, UpstreamStreamErrorEvent):
        values = _payload_error_values(exc.payload)
        value_class = _failure_class_from_error_values(values)
        if value_class is not None:
            return value_class
        return RETRY_FAILURE_QUICK_TRANSIENT
    if isinstance(exc, HTTPError):
        override = _http_retry_header_override(exc)
        if override is False:
            return RETRY_FAILURE_PERMANENT
        status = _upstream_retry_status(exc)
        values = _http_error_values(exc)
        value_class = _failure_class_from_error_values(values)
        if value_class in CAPACITY_RETRY_FAILURE_CLASSES and _status_allows_capacity_error_value(status):
            return value_class
        if value_class == RETRY_FAILURE_PERMANENT and override is not True:
            return RETRY_FAILURE_PERMANENT
        if status in PERMANENT_HTTP_ERROR_STATUSES:
            return RETRY_FAILURE_QUICK_TRANSIENT if override is True else RETRY_FAILURE_PERMANENT
        if status == 429:
            if value_class == RETRY_FAILURE_PERMANENT:
                return RETRY_FAILURE_PERMANENT
            return RETRY_FAILURE_PROVIDER_THROTTLE
        if status == 503:
            return RETRY_FAILURE_PROVIDER_OVERLOADED
        if override is True:
            return RETRY_FAILURE_QUICK_TRANSIENT
        if status in TRANSIENT_HTTP_RETRY_STATUSES:
            return RETRY_FAILURE_QUICK_TRANSIENT
        if status is not None and 520 <= status <= 599:
            return RETRY_FAILURE_QUICK_TRANSIENT
        return RETRY_FAILURE_PERMANENT
    if isinstance(
        exc,
        (
            CompactEmptyResponseError,
            IncompleteRead,
            OSError,
            TimeoutError,
            URLError,
            UpstreamStreamIdleTimeoutError,
            UpstreamStreamIncompleteError,
        ),
    ):
        return RETRY_FAILURE_QUICK_TRANSIENT
    return RETRY_FAILURE_PERMANENT


def _capacity_retry_elapsed_limit_allows(started_at: float, delay_seconds: int) -> bool:
    limit_seconds = gateway_capacity_retry_elapsed_limit_seconds()
    if limit_seconds <= 0:
        return True
    return (time.monotonic() - started_at + delay_seconds) <= limit_seconds


def _upstream_error_retryable(
    exc: BaseException,
    *,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
) -> bool:
    return _upstream_failure_class(exc) != RETRY_FAILURE_PERMANENT


def _emit_upstream_retry_event(
    event_context: Mapping[str, Any] | None,
    *,
    upstream_name: str,
    upstream_format: str,
    request_kind: str,
    attempt: int,
    max_attempts: int,
    exc: BaseException,
    delay_seconds: int,
    failure_class: str | None = None,
    failure_phase: str | None = None,
    retry_safety_class: str | None = None,
) -> None:
    identity = _retry_identity_from_context(event_context)
    resolved_failure_class = failure_class or _upstream_failure_class(exc)
    if isinstance(exc, UpstreamStreamErrorEvent):
        detail = "Upstream SSE error event"
    else:
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
    fields: dict[str, Any] = {
        "upstream": upstream_name,
        "provider_id": upstream_name,
        "upstream_format": upstream_format,
        "request_kind": request_kind,
        "retryable": True,
        "failure_class": resolved_failure_class,
        "status": _upstream_retry_status(exc),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "delay_ms": delay_seconds * 1000,
        "error": type(exc).__name__,
        "detail": detail,
        "failure_phase": failure_phase or transport_failure_phase(exc),
    }
    if retry_safety_class is not None:
        fields["retry_safety_class"] = retry_safety_class
    _write_failure_event(
        event_context,
        "upstream_retry",
        **fields,
    )


def _downstream_retry_payload(
    *,
    upstream_name: str,
    upstream_format: str,
    request_kind: str,
    attempt: int,
    max_attempts: int,
    exc: BaseException,
    delay_seconds: int,
    failure_class: str | None = None,
    failure_phase: str | None = None,
    redact_identity: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "codexhub.retry",
        "upstream": upstream_name,
        "upstream_format": upstream_format,
        "request_kind": request_kind,
        "failure_class": failure_class or _upstream_failure_class(exc),
        "status": _upstream_retry_status(exc),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "delay_ms": delay_seconds * 1000,
        "error": type(exc).__name__,
        "detail": safe_upstream_error_detail(exc, redact_identity=redact_identity),
        "failure_phase": failure_phase or transport_failure_phase(exc),
    }


@dataclass(frozen=True)
class DownstreamErrorSpec:
    inbound_format: str
    upstream_name: str
    status: int = 502
    exc: BaseException | None = None
    error: str | None = None
    detail: str | None = None
    error_type: str = "upstream_error"
    preserve_explicit_error: bool = False
    redact_identity: str | None = None


def _typed_error_code(
    *,
    error_type: str,
    error_code: str,
    exc: BaseException | None,
    status: int | None,
) -> str:
    if isinstance(exc, ModelIdentityResolutionError):
        return (
            "catalog.inconsistency"
            if exc.classification == "catalog_inconsistency"
            else "gateway.model_resolution"
        )
    if error_type == "gateway_auth_error":
        return "gateway.auth"
    if error_type == "gateway_pre_response_budget_exhausted":
        return "gateway.pre_response_budget_exhausted"
    if error_type == USER_REQUESTED_SHUTDOWN_OUTCOME:
        return "gateway.user_requested_shutdown"
    if error_type in {"invalid_request_error", "validation_error"}:
        return "provider.request"
    if error_code in {"UpstreamProtocolError", "upstream_stream_incomplete", "upstream_stream_idle_timeout"}:
        return "upstream.protocol"
    if status in {401, 403}:
        return "provider.auth"
    if status == 429:
        return "provider.rate_limit"
    if isinstance(exc, HTTPError):
        return "upstream.http"
    if isinstance(exc, (IncompleteRead, OSError, TimeoutError, URLError)):
        return "upstream.transport"
    if status is not None and status >= 500:
        return "upstream.http"
    return "upstream.error"


def _codexhub_error_payload(
    *,
    source: str,
    message: str,
    status: int | None = None,
    exc: BaseException | None = None,
    error: str | None = None,
    error_type: str = "upstream_error",
    failure_class: str | None = None,
) -> dict[str, Any]:
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    resolved_failure_class = failure_class
    if error_code == "gateway_pre_response_budget_exhausted":
        resolved_failure_class = RETRY_FAILURE_PERMANENT
    if resolved_failure_class is None and exc is not None:
        resolved_failure_class = _upstream_failure_class(exc)
    if resolved_failure_class is None and (
        error_type in {"invalid_request_error", "validation_error"}
        or (status is not None and 400 <= status < 500 and status != 429)
    ):
        resolved_failure_class = RETRY_FAILURE_PERMANENT
    if resolved_failure_class is None and (status == 429 or (status is not None and status >= 500)):
        resolved_failure_class = RETRY_FAILURE_QUICK_TRANSIENT
    if resolved_failure_class is None:
        resolved_failure_class = RETRY_FAILURE_PERMANENT
    details: dict[str, Any] = {
        "error": error_code,
        "type": error_type,
    }
    if isinstance(exc, ModelIdentityResolutionError):
        details["classification"] = exc.classification
        details["reason"] = exc.reason
        safe_provider_id = _safe_error_identity(exc.provider_id)
        safe_model_slug = _safe_error_identity(exc.model_slug)
        if safe_provider_id:
            details["provider_id"] = safe_provider_id
        if safe_model_slug:
            details["model_slug"] = safe_model_slug
    if status is not None:
        details["status"] = status
    if resolved_failure_class is not None:
        details["failure_class"] = resolved_failure_class
    return {
        "code": _typed_error_code(
            error_type=error_type,
            error_code=error_code,
            exc=exc,
            status=status,
        ),
        "message": message,
        "source": source,
        "retryable": resolved_failure_class != RETRY_FAILURE_PERMANENT,
        "details": details,
    }


def _local_gateway_auth_error_payload() -> dict[str, Any]:
    message = "missing or invalid local Gateway client key"
    return {
        "error": "unauthorized",
        "codexhub_error": _codexhub_error_payload(
            source="gateway",
            message=message,
            status=401,
            error="UnauthorizedLocalClient",
            error_type="gateway_auth_error",
            failure_class=RETRY_FAILURE_PERMANENT,
        ),
    }


def _downstream_stream_error_payload(
    *,
    upstream_name: str,
    status: int = 502,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    error_type: str = "upstream_stream_error",
    redact_identity: str | None = None,
) -> dict[str, Any]:
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamStreamError")
    if detail is not None:
        error_detail = _redact_identity_in_text(detail, redact_identity)
    elif exc is not None:
        error_detail = safe_upstream_error_detail(exc, redact_identity=redact_identity)
    else:
        error_detail = ""
    failure_class = _upstream_failure_class(exc) if exc is not None else None
    if failure_class is None and error_code in {
        "upstream_stream_idle_timeout",
        "upstream_stream_incomplete",
        "UpstreamStreamError",
        "UpstreamProtocolError",
    }:
        failure_class = RETRY_FAILURE_QUICK_TRANSIENT
    payload = {
        "type": error_type,
        "status": status,
        "upstream": upstream_name,
        "error": error_code,
        "detail": error_detail,
        "retry_owner": "client",
    }
    if failure_class is not None:
        payload["failure_class"] = failure_class
        payload["retryable"] = failure_class != RETRY_FAILURE_PERMANENT
    payload["codexhub_error"] = _codexhub_error_payload(
        source=upstream_name,
        message=error_detail or error_code,
        status=status,
        exc=exc,
        error=error_code,
        error_type=error_type,
        failure_class=failure_class,
    )
    return payload


def _downstream_sse_error_payload_for_inbound_format(error: DownstreamErrorSpec) -> dict[str, Any]:
    error_type = error.error_type
    if error_type == "upstream_error":
        error_type = "upstream_stream_error"
    if error.inbound_format == "chat_completions":
        return _chat_completion_error_payload(
            upstream_name=error.upstream_name,
            status=error.status,
            exc=error.exc,
            error=error.error,
            detail=error.detail,
            error_type=error_type,
            redact_identity=error.redact_identity,
        )
    if error.exc is not None:
        if not error.preserve_explicit_error:
            return _downstream_stream_error_payload(
                upstream_name=error.upstream_name,
                exc=error.exc,
                redact_identity=error.redact_identity,
            )
        return _downstream_stream_error_payload(
            upstream_name=error.upstream_name,
            status=error.status,
            exc=error.exc,
            error=error.error,
            detail=error.detail,
            error_type=error_type,
            redact_identity=error.redact_identity,
        )
    return _downstream_stream_error_payload(
        upstream_name=error.upstream_name,
        status=error.status,
        error=error.error or "UpstreamProtocolError",
        detail=error.detail or error.error or "upstream stream failed",
        error_type=error_type,
        redact_identity=error.redact_identity,
    )


def _responses_failed_event_for_stream_error(
    *,
    upstream_name: str,
    model: str | None,
    status: int,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    response_id: str | None = None,
    redact_identity: str | None = None,
) -> dict[str, Any]:
    stream_error = _downstream_stream_error_payload(
        upstream_name=upstream_name,
        status=status,
        exc=exc,
        error=error,
        detail=detail,
        redact_identity=redact_identity,
    )
    error_payload: dict[str, Any] = {
        "code": stream_error.get("error") or "UpstreamStreamError",
        "message": stream_error.get("detail") or stream_error.get("error") or "Upstream stream error",
        "type": stream_error.get("type") or "upstream_stream_error",
        "status": status,
        "upstream": upstream_name,
    }
    if "failure_class" in stream_error:
        error_payload["failure_class"] = stream_error["failure_class"]
    if "retryable" in stream_error:
        error_payload["retryable"] = stream_error["retryable"]
    return {
        "type": "response.failed",
        "response": {
            "id": response_id if isinstance(response_id, str) and response_id else f"resp_{uuid.uuid4().hex[:12]}",
            "object": "response",
            "status": "failed",
            "model": model,
            "output": [],
            "error": error_payload,
        },
    }


def _chat_completion_error_payload(
    *,
    upstream_name: str,
    status: int = 502,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    error_type: str = "upstream_error",
    redact_identity: str | None = None,
) -> dict[str, Any]:
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    if detail is not None:
        error_detail = _redact_identity_in_text(detail, redact_identity)
    elif exc is not None:
        error_detail = safe_upstream_error_detail(exc, redact_identity=redact_identity)
    else:
        error_detail = ""
    message = error_detail or error_code
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": error_code,
            "status": status,
            "upstream": upstream_name,
        },
        "codexhub_error": _codexhub_error_payload(
            source=upstream_name,
            message=message,
            status=status,
            exc=exc,
            error=error_code,
            error_type=error_type,
        ),
    }


def _with_codexhub_http_error(
    body: bytes,
    *,
    upstream_name: str,
    status: int,
    exc: BaseException | None = None,
) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict) or "codexhub_error" in payload:
        return body
    upstream_error = payload.get("error")
    if isinstance(upstream_error, Mapping):
        message = str(upstream_error.get("message") or upstream_error.get("detail") or "HTTPError")
        error_type = str(upstream_error.get("type") or "upstream_error")
    else:
        message = str(upstream_error or payload.get("detail") or "HTTPError")
        error_type = "upstream_error"
    payload["codexhub_error"] = _codexhub_error_payload(
        source=upstream_name,
        message=message,
        status=status,
        exc=exc,
        error="HTTPError",
        error_type=error_type,
    )
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _downstream_json_error_payload(error: DownstreamErrorSpec) -> dict[str, Any]:
    return _json_error_payload_for_inbound_format(
        inbound_format=error.inbound_format,
        upstream_name=error.upstream_name,
        status=error.status,
        exc=error.exc,
        error=error.error,
        detail=error.detail,
        error_type=error.error_type,
        redact_identity=error.redact_identity,
    )


def _json_error_payload_for_inbound_format(
    *,
    inbound_format: str,
    upstream_name: str,
    status: int,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    error_type: str = "upstream_error",
    redact_identity: str | None = None,
) -> dict[str, Any]:
    if inbound_format == "chat_completions":
        return _chat_completion_error_payload(
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
            redact_identity=redact_identity,
        )
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    if detail is not None:
        error_detail = _redact_identity_in_text(detail, redact_identity)
    elif exc is not None:
        error_detail = safe_upstream_error_detail(exc, redact_identity=redact_identity)
    else:
        error_detail = ""
    payload: dict[str, Any] = {"error": error_detail or error_code}
    if error_detail:
        payload["detail"] = error_detail
    payload["codexhub_error"] = _codexhub_error_payload(
        source=upstream_name,
        message=error_detail or error_code,
        status=status,
        exc=exc,
        error=error_code,
        error_type=error_type,
    )
    return payload


@dataclass(frozen=True)
class GatewayRequestInput:
    request_id: str
    started_at: float
    request_context: dict[str, Any]
    proxy_request_context: dict[str, Any]
    raw_provider_probe: bool
    content_length: int
    content_type: str | None
    content_encoding: str | None
    content_decoded: bool
    body: bytes
    inbound_payload: Any
    request_kind: str
    model_requested: str | None
    model: str | None
    route_reason: str


def _parse_gateway_request_input(
    handler: Any,
    *,
    inbound_format: str,
    provider_hint: str | None,
    request_id: str,
    started_at: float,
    request_context: Mapping[str, Any],
    proxy_request_context: Mapping[str, Any],
    raw_provider_probe: bool,
    content_length: int,
) -> GatewayRequestInput:
    body = handler.rfile.read(content_length)
    content_type = _get_header(handler.headers, "Content-Type")
    content_encoding = _get_header(handler.headers, "Content-Encoding")
    body, content_decoded, decode_error = decoded_request_body(body, content_encoding)
    if decode_error:
        raise ValueError(f"request body content-encoding decode failed: {decode_error}")
    try:
        inbound_payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        inbound_payload = None
    request_kind = _request_kind_from_headers_and_payload(handler.headers, inbound_payload, inbound_format)
    parsed_proxy_request_context = dict(proxy_request_context)
    if request_kind == RETRY_REQUEST_COMPACT:
        parsed_proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
        if raw_provider_probe:
            parsed_proxy_request_context["raw_provider_probe"] = True
    if isinstance(inbound_payload, Mapping) and isinstance(inbound_payload.get("model"), str):
        model_requested = inbound_payload["model"]
    else:
        model_requested = try_extract_model(body)
    model = provider_scoped_route_model(model_requested, provider_hint)
    if provider_hint is not None and not model:
        raise ValueError(f"model is required for provider path: {provider_hint}")
    route_reason = "provider_path" if provider_hint and model else "model" if model else "official_control_fallback"
    return GatewayRequestInput(
        request_id=request_id,
        started_at=started_at,
        request_context=dict(request_context),
        proxy_request_context=parsed_proxy_request_context,
        raw_provider_probe=raw_provider_probe,
        content_length=content_length,
        content_type=content_type,
        content_encoding=content_encoding,
        content_decoded=content_decoded,
        body=body,
        inbound_payload=inbound_payload,
        request_kind=request_kind,
        model_requested=model_requested,
        model=model,
        route_reason=route_reason,
    )


def _remaining_pre_response_budget_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _clamp_timeout_to_pre_response_budget(
    timeout: int | float,
    deadline: float | None,
    *,
    phase: str,
    attempt: int | None = None,
) -> int | float:
    remaining = _remaining_pre_response_budget_seconds(deadline)
    if remaining is None:
        return timeout
    if remaining <= 0:
        raise GatewayPreResponseBudgetExhausted(phase=phase, attempt=attempt)
    return min(float(timeout), remaining)


def _require_retry_delay_within_pre_response_budget(
    deadline: float | None,
    delay_seconds: int | float,
    *,
    phase: str,
    attempt: int | None = None,
) -> None:
    remaining = _remaining_pre_response_budget_seconds(deadline)
    if remaining is not None and delay_seconds >= remaining:
        raise GatewayPreResponseBudgetExhausted(phase=phase, attempt=attempt)


def _open_upstream_once(
    request: Request,
    *,
    upstream_name: str,
    timeout: int | float,
    transport_policy: TransportPolicy | None = None,
) -> Any:
    selected_transport = transport_policy or (
        TransportPolicy.OFFICIAL_KEEPALIVE
        if upstream_name == "official"
        else TransportPolicy.STANDARD
    )
    if selected_transport == TransportPolicy.OFFICIAL_KEEPALIVE:
        return _official_urlopen(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


def _open_upstream_response(
    request: Request,
    *,
    upstream_name: str,
    upstream_format: str,
    timeout: int | float,
    event_context: Mapping[str, Any] | None = None,
    downstream_retry_callback: Any = None,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
    max_attempts: int | None = None,
    retry_policy: str = RETRY_GATEWAY_FULL,
    retry_http_errors: bool = True,
    retry_execution: RetryExecutionPlan | None = None,
    transport_policy: TransportPolicy | None = None,
    downstream_exposed: Callable[[], bool] | None = None,
    pre_response_deadline: float | None = None,
    open_attempt_budget: dict[str, int] | None = None,
) -> Any:
    if retry_execution is not None:
        request_kind = retry_execution.request_kind
        retry_policy = retry_execution.policy.value
        retry_http_errors = retry_execution.retry_http_errors
        timeout = retry_execution.request_timeout_seconds
        base_retry_attempts = retry_execution.base_open_attempts
        explicit_max_attempts = (
            retry_execution.open_attempt_budget is not None
        )
        if open_attempt_budget is None:
            open_attempt_budget = retry_execution.new_open_attempt_budget()
    else:
        explicit_max_attempts = max_attempts is not None
        base_retry_attempts = (
            _upstream_retry_attempts(request_kind)
            if max_attempts is None
            else max(1, max_attempts)
        )
    if open_attempt_budget is not None:
        remaining_open_attempts = max(
            0,
            open_attempt_budget["max_attempts"] - open_attempt_budget["attempts_started"],
        )
        if remaining_open_attempts <= 0:
            raise GatewayPreResponseBudgetExhausted(
                phase="upstream_open_attempts",
                attempt=open_attempt_budget["attempts_started"],
            )
        base_retry_attempts = min(base_retry_attempts, remaining_open_attempts)
    retry_started_at = time.monotonic()
    diagnostic_request_key = _diagnostic_context_value(event_context, "request_id")
    diagnostic_model = _diagnostic_context_value(event_context, "model")
    model_access_path = _model_access_path_from_event_context(
        event_context,
        upstream_name,
        upstream_format,
    )
    attempt = 1
    while True:
        if open_attempt_budget is not None:
            request_attempt = open_attempt_budget["attempts_started"] + 1
            request_retry_budget = open_attempt_budget["max_attempts"]
        else:
            request_attempt = attempt
            request_retry_budget = base_retry_attempts
        admission = _active_gateway_request()
        if admission is not None:
            admission.raise_if_cancelled()
        if _model_access_path_idempotency_guaranteed(model_access_path):
            _ensure_retry_attempt_identity(
                event_context if isinstance(event_context, dict) else None,
                request,
                model_access_path,
            )
        attempt_timeout = _clamp_timeout_to_pre_response_budget(
            timeout,
            pre_response_deadline,
            phase="upstream_open",
            attempt=request_attempt,
        )
        if open_attempt_budget is not None:
            open_attempt_budget["attempts_started"] = request_attempt
        attempt_started_at = time.monotonic()
        try:
            response = _open_upstream_once(
                request,
                upstream_name=upstream_name,
                timeout=attempt_timeout,
                transport_policy=transport_policy,
            )
            remaining_budget = _remaining_pre_response_budget_seconds(pre_response_deadline)
            if remaining_budget is not None and remaining_budget <= 0:
                close_response = getattr(response, "close", None)
                if callable(close_response):
                    try:
                        close_response()
                    except Exception:
                        pass
                raise GatewayPreResponseBudgetExhausted(
                    phase="response_headers",
                    attempt=request_attempt,
                )
            if admission is not None:
                admission.attach_upstream_transport(response)
                admission.raise_if_cancelled()
            elapsed_ms = int(max(0.0, time.monotonic() - attempt_started_at) * 1000)
            connection_disposition = _diagnostic_connection_disposition(response)
            # A returned response proves this Gateway attempt reached response
            # completion after writing its request. It cannot prove DNS, TCP,
            # or TLS occurred for this attempt (especially on a reused lease),
            # so those success phases remain absent unless a lower-level seam
            # later exposes them.
            _observe_gateway_diagnostic(
                "observe_upstream_phase",
                diagnostic_request_key,
                phase="upstream_request_write",
                attempt=request_attempt,
                retry_budget=request_retry_budget,
                elapsed_ms=elapsed_ms,
                outcome="ok",
                provider=upstream_name,
                model=diagnostic_model,
            )
            _observe_gateway_diagnostic(
                "observe_upstream_attempt",
                diagnostic_request_key,
                attempt=request_attempt,
                retry_budget=request_retry_budget,
                elapsed_ms=elapsed_ms,
                outcome="ok",
                connection_disposition=connection_disposition,
                provider=upstream_name,
                model=diagnostic_model,
            )
            diagnostic_status, diagnostic_headers = _diagnostic_response_metadata(response)
            _observe_gateway_diagnostic(
                "observe_upstream_headers",
                diagnostic_request_key,
                status=diagnostic_status,
                headers=diagnostic_headers,
            )
            return response
        except GatewayPreResponseBudgetExhausted:
            raise
        except (HTTPError, IncompleteRead, OSError, URLError) as exc:
            if admission is not None:
                admission.raise_if_cancelled()
            elapsed_ms = int(max(0.0, time.monotonic() - attempt_started_at) * 1000)
            connection_disposition = _diagnostic_error_connection_disposition(exc)
            try:
                transport_phase = transport_failure_phase(exc)
            except Exception:
                transport_phase = "unknown"
            # The conservative retry-safety phase is authoritative for the
            # request-scoped retry decision and for any retry telemetry that
            # downstream consumers may treat as classification evidence.  The
            # best-effort transport phase is retained only for low-level
            # diagnostics that are explicitly marked as heuristic.
            retry_safety_failure_phase = _retry_safety_failure_phase(exc) or "unknown"
            apply_retry_safety = (
                upstream_name != "official"
                and request_kind == RETRY_REQUEST_MAIN_GENERATION
                and request.get_method() == "POST"
            )
            telemetry_failure_phase = retry_safety_failure_phase if apply_retry_safety else transport_phase
            diagnostic_phase = _diagnostic_transport_phase(transport_phase)
            if diagnostic_phase is not None:
                _observe_gateway_diagnostic(
                    "observe_upstream_phase",
                    diagnostic_request_key,
                    phase=diagnostic_phase,
                    attempt=request_attempt,
                    retry_budget=request_retry_budget,
                    elapsed_ms=elapsed_ms,
                    outcome="error",
                    failure_phase=transport_phase,
                    provider=upstream_name,
                    model=diagnostic_model,
                )
            if isinstance(exc, HTTPError) and not retry_http_errors:
                _observe_gateway_diagnostic(
                    "observe_upstream_attempt",
                    diagnostic_request_key,
                    attempt=request_attempt,
                    retry_budget=request_attempt,
                    elapsed_ms=elapsed_ms,
                    outcome="error",
                    failure_phase=telemetry_failure_phase,
                    connection_disposition=connection_disposition,
                    provider=upstream_name,
                    model=diagnostic_model,
                )
                raise
            failure_class = _upstream_failure_class(exc)
            downstream_exposed_now = bool(downstream_exposed is not None and downstream_exposed())
            retry_safety_class = _retry_safety_class(
                exc,
                request=request,
                upstream_name=upstream_name,
                request_kind=request_kind,
                downstream_exposed=downstream_exposed_now,
                model_access_path=model_access_path,
                failure_phase=retry_safety_failure_phase,
            )
            retry_attempts = (
                (
                    min(
                        base_retry_attempts,
                        retry_execution.open_attempts_for_failure_class(
                            failure_class
                        ),
                    )
                    if open_attempt_budget is not None
                    else retry_execution.open_attempts_for_failure_class(
                        failure_class
                    )
                )
                if retry_execution is not None
                else _retry_attempts_for_failure_class(
                    request_kind=request_kind,
                    base_attempts=base_retry_attempts,
                    failure_class=failure_class,
                    explicit_max_attempts=explicit_max_attempts,
                )
            )
            error_retry_budget = (
                open_attempt_budget["max_attempts"]
                if open_attempt_budget is not None
                else retry_attempts
            )
            if retry_safety_class in _SUPPRESSED_RETRY_SAFETY_CLASSES:
                _observe_gateway_diagnostic(
                    "observe_upstream_attempt",
                    diagnostic_request_key,
                    attempt=request_attempt,
                    retry_budget=error_retry_budget,
                    elapsed_ms=elapsed_ms,
                    outcome="error",
                    failure_phase=telemetry_failure_phase,
                    connection_disposition=connection_disposition,
                    provider=upstream_name,
                    model=diagnostic_model,
                )
                remaining_budget = _remaining_pre_response_budget_seconds(pre_response_deadline)
                if remaining_budget is not None and remaining_budget <= 0:
                    raise GatewayPreResponseBudgetExhausted(
                        phase=telemetry_failure_phase,
                        attempt=request_attempt,
                    ) from exc
                _emit_upstream_retry_suppressed_event(
                    event_context,
                    upstream_name=upstream_name,
                    upstream_format=upstream_format,
                    request_kind=request_kind,
                    attempt=request_attempt,
                    max_attempts=error_retry_budget,
                    exc=exc,
                    failure_class=failure_class,
                    failure_phase=telemetry_failure_phase,
                    retry_safety_class=retry_safety_class,
                )
                raise
            _observe_gateway_diagnostic(
                "observe_upstream_attempt",
                diagnostic_request_key,
                attempt=request_attempt,
                retry_budget=error_retry_budget,
                elapsed_ms=elapsed_ms,
                outcome="error",
                failure_phase=telemetry_failure_phase,
                connection_disposition=connection_disposition,
                provider=upstream_name,
                model=diagnostic_model,
            )
            remaining_budget = _remaining_pre_response_budget_seconds(pre_response_deadline)
            if remaining_budget is not None and remaining_budget <= 0:
                raise GatewayPreResponseBudgetExhausted(
                    phase=telemetry_failure_phase,
                    attempt=request_attempt,
                ) from exc
            if attempt >= retry_attempts or failure_class == RETRY_FAILURE_PERMANENT:
                raise
            delay_seconds = (
                retry_execution.retry_delay_seconds(
                    request_attempt,
                    failure_class=failure_class,
                    exc=exc,
                )
                if retry_execution is not None
                else gateway_retry_delay_seconds(
                    request_attempt,
                    failure_class=failure_class,
                    exc=exc,
                )
            )
            retry_elapsed_seconds = max(
                0.0,
                time.monotonic() - retry_started_at,
            )
            if (
                failure_class in CAPACITY_RETRY_FAILURE_CLASSES
                and not (
                    retry_execution.capacity_elapsed_limit_allows(
                        retry_elapsed_seconds,
                        delay_seconds,
                    )
                    if retry_execution is not None
                    else _capacity_retry_elapsed_limit_allows(
                        retry_started_at,
                        delay_seconds,
                    )
                )
            ):
                raise
            _require_retry_delay_within_pre_response_budget(
                pre_response_deadline,
                delay_seconds,
                phase="retry_delay",
                attempt=request_attempt,
            )
            _emit_upstream_retry_event(
                event_context,
                upstream_name=upstream_name,
                upstream_format=upstream_format,
                request_kind=request_kind,
                attempt=request_attempt,
                max_attempts=error_retry_budget,
                exc=exc,
                delay_seconds=delay_seconds,
                failure_class=failure_class,
                failure_phase=telemetry_failure_phase,
                retry_safety_class=retry_safety_class,
            )
            if downstream_retry_callback is not None and retry_policy != RETRY_CONSERVATIVE_PRE_OUTPUT:
                if not downstream_retry_callback(
                    _downstream_retry_payload(
                        upstream_name=upstream_name,
                        upstream_format=upstream_format,
                        request_kind=request_kind,
                        attempt=request_attempt,
                        max_attempts=error_retry_budget,
                        exc=exc,
                        failure_phase=telemetry_failure_phase,
                        delay_seconds=delay_seconds,
                        failure_class=failure_class,
                        redact_identity=_retry_identity_from_context(event_context),
                    )
                ):
                    raise DownstreamClosedBeforeRetryError("downstream closed before upstream retry")
            _sleep_for_retry_with_gateway_cancellation(delay_seconds)
            attempt += 1


def _responses_synthetic_terminal_failure(
    handler: CodexProxyHandler,
    exc: BaseException,
    *,
    status: int,
    response_id: str | None,
    upstream_name: str,
    model: str | None,
    redact_identity: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Write a Responses-format synthetic terminal failure event."""
    if not handler._write_sse_event(
        "response.failed",
        _responses_failed_event_for_stream_error(
            upstream_name=upstream_name,
            model=model,
            status=status,
            exc=exc,
            response_id=response_id,
            redact_identity=redact_identity,
        ),
    ):
        seam = _handler_downstream_stream_commit(handler)
        write_exc = seam.last_write_error() if seam is not None else None
        if write_exc is None:
            write_exc = OSError("downstream closed")
        return False, type(write_exc).__name__, safe_upstream_error_detail(write_exc, redact_identity=redact_identity)
    return True, None, None


# Downstream writes that do not participate in the stream-commit seam.
# Non-streaming JSON responses, WebSocket handshakes/frames, and non-streaming
# body-relay writes are intentionally allowlisted: they either carry no SSE
# terminal semantics or are complete payloads whose lifecycle is bounded by the
# calling context. All production SSE headers/bodies must be authorized by the
# request-scoped _GatewayDownstreamStreamCommit seam.
DOWNSTREAM_STREAM_COMMIT_ALLOWLIST: frozenset[str] = frozenset({
    "_send_json",
    "_safe_send_json",
    "_send_local_responses_no_content",
    "_send_method_not_allowed",
    "_reject_local_responses_websocket_probe",
    "_handle_websocket_recording_probe",
    "_write_non_streaming_body_relay",
    "_write_sse_bytes",
})

# Low-level seam methods that are the only legitimate direct ``self.wfile.write``
# callers inside the stream-commit seam. They are kept separate from the allowlist
# because they participate in (rather than bypass) the seam.
DOWNSTREAM_STREAM_COMMIT_SEAM_METHODS: frozenset[str] = frozenset({
    "commit_data",
    "commit_sse_bytes",
    "commit_terminal_failure",
})


def _handler_downstream_stream_commit(handler: Any) -> _GatewayDownstreamStreamCommit | None:
    """Return the request-scoped stream-commit seam bound to ``handler`` if active."""
    seam = getattr(handler, "_downstream_stream_commit", None)
    return seam if isinstance(seam, _GatewayDownstreamStreamCommit) else None


class _GatewayDownstreamStreamCommit:
    """Owns downstream writes and terminal commitment for an SSE stream.

    The seam is the single owner of all bytes written to the downstream client for
    a passthrough SSE stream: data events, terminal events, and sanitized error
    events. It tracks whether a terminal event has been committed to the
    downstream, classifies the close phase (before output, during an event, or
    after terminal commitment), and hands off cancellation/closure to the owned
    upstream response so that no later write, retry, fallback, or duplicate
    terminal can occur on this stream.

    Protocol-specific observers (terminal detection, usage extraction, and
    synthetic terminal formatting) are injected by the caller; the seam itself
    owns only the lifecycle ledger and byte commitment.

    When promoted to request scope the seam may be created before the upstream
    response is opened. The upstream response is attached later via
    ``attach_upstream_response`` so cancellation or a downstream write failure
    can still close the owned upstream work.
    """

    def __init__(
        self,
        handler: CodexProxyHandler,
        upstream_response: Any | None,
        upstream_name: str,
        *,
        model: str | None = None,
        request_id: str | None = None,
        inbound_format: str = "responses",
        upstream_format: str = "responses",
        terminal_observer: Callable[[str | None, bytes, Any], bool] | None = None,
        usage_line_callback: Callable[[Mapping[str, Any], bytes], None] | None = None,
        synthetic_terminal_failure_callback: Callable[
            [CodexProxyHandler, BaseException, int, str | None, str, str | None],
            tuple[bool, str | None, str | None],
        ]
        | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self._handler = handler
        self._upstream_response = upstream_response
        self._upstream_name = upstream_name
        self._model = model
        self._request_id = request_id
        self._inbound_format = inbound_format
        self._upstream_format = upstream_format
        self._usage_line_callback = (
            usage_line_callback
            if usage_line_callback is not None
            else _offer_official_passthrough_usage_line
        )
        self._synthetic_terminal_failure_callback = synthetic_terminal_failure_callback
        self._max_frame_bytes = max_frame_bytes
        self._sse_stats = PassthroughSseSemanticStats(
            terminal_observer=terminal_observer,
            max_frame_bytes=max_frame_bytes,
        )
        self._terminal_observed = False
        self._terminal_committed = False
        self._downstream_closed = False
        self._downstream_output_started = False
        self._downstream_content_exposed = False
        self._terminal_drain_timeout_shortened = False
        self._lines_streamed = 0
        self._bytes_streamed = 0
        self._last_upstream_byte_at: float | None = None
        self._last_write_error: OSError | None = None
        self._last_successful_completion_bytes = b""
        self._headers_committed = False
        self._ensure_headers_committed_callback: Callable[[], bool] | None = None

    @property
    def terminal_committed(self) -> bool:
        return self._terminal_committed

    @property
    def downstream_closed(self) -> bool:
        return self._downstream_closed

    @property
    def close_phase(self) -> str:
        if self._terminal_committed:
            return "after_terminal"
        if self._downstream_output_started:
            return "during_event"
        return "before_output"

    def stats(self) -> dict[str, Any]:
        self._sse_stats.finalize_pending()
        return self._sse_stats.fields()

    def attach_upstream_response(self, response: Any) -> None:
        """Bind the upstream response after the seam has been created."""
        if self._downstream_closed:
            self._close_upstream_response(response)
            return
        self._upstream_response = response

    def set_upstream_format(self, upstream_format: str) -> None:
        """Update the active protocol before a planned fallback is opened."""
        if self._downstream_output_started or self._terminal_committed:
            return
        self._upstream_format = upstream_format

    def mark_downstream_content_exposed(self) -> None:
        """Record that upstream response content has been produced for the client.

        This is semantic exposure: it is set when the upstream emits visible or
        tool output that would be relayed downstream, even if the bytes are still
        buffered in the relay layer and have not yet been written to the socket.
        """
        self._downstream_content_exposed = True

    def set_terminal_observer(self, terminal_observer: Callable[[str | None, bytes, Any], bool] | None) -> None:
        self._sse_stats = PassthroughSseSemanticStats(
            terminal_observer=terminal_observer,
            max_frame_bytes=self._max_frame_bytes,
        )

    def set_usage_line_callback(self, usage_line_callback: Callable[[Mapping[str, Any], bytes], None] | None) -> None:
        self._usage_line_callback = (
            usage_line_callback
            if usage_line_callback is not None
            else _offer_official_passthrough_usage_line
        )

    def set_synthetic_terminal_failure_callback(
        self,
        synthetic_terminal_failure_callback: Callable[
            [CodexProxyHandler, BaseException, int, str | None, str, str | None],
            tuple[bool, str | None, str | None],
        ]
        | None,
    ) -> None:
        self._synthetic_terminal_failure_callback = synthetic_terminal_failure_callback

    def set_ensure_headers_committed_callback(
        self,
        callback: Callable[[], bool] | None,
    ) -> None:
        self._ensure_headers_committed_callback = (
            None if self._headers_committed else callback
        )

    def _ensure_headers_committed_before_write(self) -> bool:
        if self._headers_committed:
            self._ensure_headers_committed_callback = None
            return True
        callback = self._ensure_headers_committed_callback
        if callback is None:
            return True
        if not callback():
            return False
        self._ensure_headers_committed_callback = None
        return True

    def _close_upstream_response(self, response: Any) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _close_upstream(self) -> None:
        response = self._upstream_response
        if response is None:
            return
        self._close_upstream_response(response)

    def close(self) -> None:
        """Close the downstream and upstream sides; idempotent."""
        if self._downstream_closed:
            return
        self._downstream_closed = True
        self._handler.close_connection = True
        self._close_upstream()

    def cancel(self) -> None:
        """Hand off cancellation: close downstream and upstream once."""
        self.close()

    def _record_terminal(self) -> None:
        if self._terminal_drain_timeout_shortened:
            return
        shorten = getattr(self._upstream_response, "shorten_terminal_drain_timeout", None)
        if callable(shorten):
            try:
                shorten(OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS)
            except Exception:
                pass
        self._terminal_drain_timeout_shortened = True

    def _observe_line(self, line: bytes) -> bool:
        """Observe one raw SSE line and return True if it contains a terminal event."""
        self._last_upstream_byte_at = time.monotonic()
        self._sse_stats.observe_bytes(line)
        if self._sse_stats.terminal_event_seen and not self._terminal_observed:
            self._terminal_observed = True
            return True
        return False

    def commit_data(self, line: bytes) -> bool:
        """Commit one upstream SSE line to the downstream stream.

        Returns True if the line was written (or was empty). Returns False if the
        downstream stream is closed or a terminal event has already been committed,
        in which case the caller must stop writing.
        """
        if self._downstream_closed:
            return False
        if not line:
            return True
        if self._terminal_committed:
            return False
        if not self._ensure_headers_committed_before_write():
            return False
        terminal_observed_now = self._observe_line(line)
        if terminal_observed_now:
            _observe_gateway_diagnostic("observe_terminal", self._request_id, forwarded=False)
        try:
            self._handler.wfile.write(line)
            self._handler.wfile.flush()
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False
        self._downstream_output_started = True
        self._lines_streamed += 1
        self._bytes_streamed += len(line)
        self._last_successful_completion_bytes = self._sse_stats.pending_completion_bytes()
        if terminal_observed_now:
            self._terminal_committed = True
            _observe_gateway_diagnostic("observe_terminal", self._request_id, forwarded=True)
            self._record_terminal()
            # Terminal ledger is sealed: close the downstream side deterministically
            # so no later upstream byte can be written or mislabeled as a disconnect.
            # Upstream is left open so the caller can drain to the natural EOF.
            self._downstream_closed = True
            self._handler.close_connection = True
        self._usage_line_callback(
            {
                "request_id": self._request_id,
                "model": self._model,
                "upstream": self._upstream_name,
                "upstream_format": self._upstream_format,
                "inbound_format": self._inbound_format,
            },
            line,
        )
        return True

    def commit_headers(self, status: int, send_headers: Callable[[], None]) -> bool:
        """Authorize and record the sending of HTTP response headers.

        ``send_headers`` is called only when the stream is still open and no
        terminal has been committed. A successful commit owns the one allowed
        header block and disarms any deferred header callback. Later header
        requests are successful no-ops. Any OSError is captured, closes the
        owned upstream work, and seals the downstream side.
        """
        if self._headers_committed:
            self._ensure_headers_committed_callback = None
            return True
        if self._downstream_closed or self._terminal_committed:
            return False
        try:
            send_headers()
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False
        self._headers_committed = True
        self._ensure_headers_committed_callback = None
        return True

    def commit_sse_bytes(self, data: bytes, *, observe: bool = True) -> bool:
        """Commit constructed SSE bytes to the downstream stream.

        This is used for retry diagnostics, converted-route events, keepalives,
        and error events that are produced above the raw upstream line layer.
        When ``observe`` is True the bytes are inspected for terminal events.
        """
        if self._downstream_closed:
            return False
        if not data:
            return True
        if self._terminal_committed:
            return False
        if not self._ensure_headers_committed_before_write():
            return False
        terminal_observed_now = False
        if observe:
            terminal_observed_now = self._observe_line(data)
            if terminal_observed_now:
                _observe_gateway_diagnostic("observe_terminal", self._request_id, forwarded=False)
        try:
            self._handler.wfile.write(data)
            self._handler.wfile.flush()
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False
        self._downstream_output_started = True
        self._lines_streamed += 1
        self._bytes_streamed += len(data)
        if observe:
            self._last_successful_completion_bytes = self._sse_stats.pending_completion_bytes()
        if terminal_observed_now:
            self._terminal_committed = True
            _observe_gateway_diagnostic("observe_terminal", self._request_id, forwarded=True)
            self._record_terminal()
            self._downstream_closed = True
            self._handler.close_connection = True
        return True

    def commit_terminal_failure(
        self,
        exc: BaseException,
        *,
        status: int = 502,
    ) -> tuple[bool, str | None, str | None]:
        """Commit a synthetic terminal failure event if terminal is not committed.

        The pending upstream SSE event is first finalized with a blank-line
        boundary so that the synthetic terminal failure is a distinct, valid
        SSE frame. The response ID is taken from the finalized event stream if
        available.

        Returns (sent, write_error_name, write_error_detail). No event is written
        if the stream is already closed, a terminal has already been committed,
        or no synthetic terminal failure callback is configured, which prevents
        duplicate terminals and fallback writes.
        """
        if self._downstream_closed or self._terminal_committed:
            return False, None, None
        if self._synthetic_terminal_failure_callback is None:
            return False, None, None
        if not self._ensure_headers_committed_before_write():
            return False, None, None
        try:
            size_limit_exceeded = isinstance(exc, SseFrameTooLargeError)
            completion = (
                self._last_successful_completion_bytes
                if size_limit_exceeded
                else self._sse_stats.pending_completion_bytes()
            )
            if completion:
                self._handler.wfile.write(completion)
                self._handler.wfile.flush()
                if not size_limit_exceeded:
                    self._sse_stats.observe_bytes(completion)
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False, type(write_exc).__name__, safe_upstream_error_detail(write_exc)
        response_id = self._sse_stats.response_id
        try:
            (
                synthetic_terminal_event_sent,
                synthetic_terminal_write_error,
                synthetic_terminal_write_detail,
            ) = self._synthetic_terminal_failure_callback(
                self._handler,
                exc,
                status=status,
                response_id=response_id,
                upstream_name=self._upstream_name,
                model=self._model,
            )
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False, type(write_exc).__name__, safe_upstream_error_detail(write_exc)
        if synthetic_terminal_event_sent:
            self._terminal_committed = True
            self._downstream_closed = True
            self._handler.close_connection = True
        return (
            synthetic_terminal_event_sent,
            synthetic_terminal_write_error,
            synthetic_terminal_write_detail,
        )

    def last_write_error(self) -> OSError | None:
        return self._last_write_error

    def counters(self) -> dict[str, Any]:
        return {
            "lines_streamed": self._lines_streamed,
            "bytes_streamed": self._bytes_streamed,
            "last_upstream_byte_at": self._last_upstream_byte_at,
        }


class _UpstreamSseReaderLifecycle:
    """Owns one upstream SSE reader thread, a bounded queue, and deterministic close/join.

    This lifecycle is the single owner of the thread that reads raw SSE lines from
    an upstream response. It uses a bounded queue (capacity 32) so a slow or stalled
    downstream cannot create unbounded buffering. The producer observes close while
    waiting on a full queue; the consumer observes close while waiting on an empty
    queue. Close is idempotent and wakes both sides. Join is bounded and classifies a
    non-terminating reader without hiding it.
    """

    QUEUE_CAPACITY = 32
    PRODUCER_PUT_TIMEOUT_SECONDS = 0.05
    CONSUMER_POLL_TIMEOUT_SECONDS = 0.1
    JOIN_TIMEOUT_SECONDS = 1.0

    def __init__(
        self,
        response: Any,
        *,
        admission: GatewayRequestAdmission | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
        thread_name: str = "codex-proxy-sse-reader",
    ) -> None:
        self._response = response
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=self.QUEUE_CAPACITY)
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name
        self._cancellation_requested = (
            (lambda: admission.cancelled) if admission is not None else cancellation_requested
        )
        self._started = False
        self._response_closed = False
        self._join_outcome: str | None = None
        if admission is not None:
            admission.attach_upstream_transport(self)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def reader_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def join_outcome(self) -> str | None:
        return self._join_outcome

    def _cancelled(self) -> bool:
        if self._closed.is_set():
            return True
        cancellation_requested = self._cancellation_requested
        if cancellation_requested is None:
            return False
        try:
            return bool(cancellation_requested())
        except Exception:
            return False

    def start(self) -> None:
        """Start the reader thread idempotently."""
        with self._close_lock:
            if self._started or self._closed.is_set():
                return
            self._started = True
            thread = threading.Thread(target=self._read_upstream, name=self._thread_name, daemon=True)
            self._thread = thread
        thread.start()

    def _read_upstream(self) -> None:
        """Producer loop: read lines and enqueue them with bounded backpressure."""
        response = self._response
        try:
            while not self._cancelled():
                try:
                    line = response.readline()
                except BaseException as exc:
                    if not self._cancelled():
                        self._enqueue(("error", exc))
                    return
                if not self._enqueue(("line", line)):
                    return
                if not line:
                    return
        finally:
            self.close()

    def _enqueue(self, item: tuple[str, Any]) -> bool:
        """Enqueue one item, respecting close/cancellation. Returns False if closed."""
        while not self._cancelled():
            try:
                self._queue.put(item, timeout=self.PRODUCER_PUT_TIMEOUT_SECONDS)
                return True
            except queue.Full:
                continue
        return False

    def get(self, timeout: float | None = None) -> tuple[str, Any]:
        """Get one queued item.

        Raises ``queue.Empty`` on timeout. The caller should check :attr:`closed`
        to distinguish a transient empty queue from a closed lifecycle.
        """
        self.start()
        if timeout is not None:
            try:
                return self._queue.get(timeout=max(0.0, timeout))
            except queue.Empty:
                if self._cancelled():
                    return "line", b""
                raise
        while True:
            try:
                return self._queue.get(timeout=self.CONSUMER_POLL_TIMEOUT_SECONDS)
            except queue.Empty:
                if self._cancelled():
                    return "line", b""

    def readline(self) -> bytes:
        """Read one upstream SSE line.

        Returns ``b""`` when the lifecycle is closed. Raises the stored upstream
        exception when the reader encountered an error.
        """
        self.start()
        while True:
            kind, value = self.get()
            if kind == "error":
                raise value
            return value

    def iter_lines(self):
        """Yield raw upstream SSE lines until EOF or close."""
        try:
            while True:
                line = self.readline()
                yield line
                if not line:
                    return
        finally:
            self.close()

    def shorten_terminal_drain_timeout(self, timeout_seconds: float) -> None:
        """Forward the existing pooled-response terminal drain optimization."""
        shorten = getattr(self._response, "shorten_terminal_drain_timeout", None)
        if callable(shorten):
            shorten(timeout_seconds)

    def close(self) -> None:
        """Close the lifecycle idempotently and wake both producer and consumer."""
        with self._close_lock:
            self._closed.set()
            should_close_response = not self._response_closed
            self._response_closed = True
        if should_close_response:
            close = getattr(self._response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def join(self, timeout: float = JOIN_TIMEOUT_SECONDS) -> tuple[bool, str | None]:
        """Join the reader thread for at most ``timeout`` seconds.

        Returns ``(joined, outcome)``. A started reader receives a sanitized
        termination classification; ``outcome`` is ``None`` only when no reader
        was started. Once a join timeout is observed, that classification remains
        retained even if a later join observes termination.
        """
        thread = self._thread
        if thread is None:
            return True, None
        bounded_timeout = min(self.JOIN_TIMEOUT_SECONDS, max(0.0, timeout))
        thread.join(timeout=bounded_timeout)
        if thread.is_alive():
            outcome = "upstream_sse_reader_thread_did_not_terminate"
            if self._join_outcome != outcome:
                logger.warning("upstream SSE reader join ended with %s", outcome)
            self._join_outcome = outcome
            return False, self._join_outcome
        if self._join_outcome is None:
            self._join_outcome = "upstream_sse_reader_thread_terminated"
        return True, self._join_outcome


class CodexProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        self._diagnostic_request_id: str | None = None
        try:
            super().handle_one_request()
        finally:
            self._diagnostic_request_id = None

    def _observe_downstream_phase(self, event: str, *, status: int | None = None) -> None:
        request_id = getattr(self, "_diagnostic_request_id", None)
        if not isinstance(request_id, str) or not request_id:
            return
        fields: dict[str, Any] = {"request_id": request_id}
        if isinstance(status, int):
            fields["status"] = status
        _observe_gateway_diagnostic("observe_proxy_event", event, fields)

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        self._observe_downstream_phase("downstream_response_open", status=code)

    def end_headers(self) -> None:
        super().end_headers()
        self._observe_downstream_phase("downstream_headers")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if _is_websocket_upgrade(self.headers) and gateway_websocket_recorder_enabled():
            self._handle_websocket_recording_probe()
            return
        if parsed.path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "build": PROXY_BUILD,
                    "features": PROXY_FEATURES,
                },
            )
            return
        if parsed.path == "/v1/models":
            self._send_json(200, current_catalog_data())
            return
        if parsed.path == "/v1/responses":
            if _is_websocket_upgrade(self.headers):
                self._reject_local_responses_websocket_probe()
                return
            self._send_local_responses_no_content()
            return
        if parsed.path.startswith("/v1/responses/"):
            self._passthrough_official_control_request("GET")
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/shutdown":
            request_context = request_context_from_headers(self.headers)
            if not _local_request_authorized(self.headers, request_context):
                self._send_json(401, _local_gateway_auth_error_payload())
                self.close_connection = True
                return
            controller = _gateway_shutdown_controller_for_handler(self)
            controller.close_admission()
            self._send_json(
                200,
                {
                    "ok": True,
                    "outcome": USER_REQUESTED_SHUTDOWN_OUTCOME,
                },
            )
            self.close_connection = True
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if parsed.path == "/v1/responses":
            self._proxy_post_request(inbound_format="responses")
            return
        provider_hint = provider_scoped_path(parsed.path, "responses")
        if provider_hint is not None:
            self._proxy_post_request(inbound_format="responses", provider_hint=provider_hint)
            return

        if parsed.path == "/v1/chat/completions":
            self._proxy_post_request(inbound_format="chat_completions")
            return
        provider_hint = provider_scoped_path(parsed.path, "chat/completions")
        if provider_hint is not None:
            self._proxy_post_request(inbound_format="chat_completions", provider_hint=provider_hint)
            return

        if parsed.path == "/v1/images/generations":
            self._proxy_official_image_generation()
            return

        self._send_json_and_close(404, {"error": "not found"})

    def _proxy_official_image_generation(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        if not _local_request_authorized(self.headers, request_context):
            write_proxy_event(
                "request_error",
                request_id=request_id,
                path=self.path,
                method="POST",
                model=None,
                upstream="local",
                route_reason="official_image_generation",
                status=401,
                error="UnauthorizedLocalClient",
                detail="missing or invalid local Gateway client key",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._send_json_and_close(401, _local_gateway_auth_error_payload())
            return

        shutdown_controller = _gateway_shutdown_controller_for_handler(self)
        admission = shutdown_controller.admit()
        if admission is None:
            self._send_json_and_close(503, user_requested_shutdown_payload("responses"))
            return
        previous_admission = _activate_gateway_request(admission)
        upstream = official_upstream()
        upstream_name = str(upstream["name"])
        status = 500
        try:
            admission.raise_if_cancelled()
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self._send_json_and_close(400, {"error": "invalid Content-Length"})
                return
            if content_length < 0:
                self._send_json_and_close(400, {"error": "invalid Content-Length"})
                return
            max_body_bytes = max_request_body_bytes()
            if content_length > max_body_bytes:
                self._send_json_and_close(
                    413,
                    {
                        "error": "request body too large",
                        "max_request_body_bytes": max_body_bytes,
                    },
                )
                return

            body = self.rfile.read(content_length)
            admission.raise_if_cancelled()
            operational_authentication = materialize_operational_authentication(
                self.headers,
                upstream,
            )
            headers = upstream_headers(
                self.headers,
                upstream,
                request_mutation_policy=MutationPolicy.OFFICIAL_PASSTHROUGH,
                operational_authentication=operational_authentication,
            )
            request = Request(
                _upstream_endpoint_url(upstream, "/images/generations"),
                data=body,
                headers=headers,
                method="POST",
            )
            event_context = {
                "request_id": request_id,
                "model": None,
                **_event_context_with_request_kind(
                    request_context,
                    RETRY_REQUEST_MAIN_GENERATION,
                ),
            }
            write_proxy_event(
                "request_start",
                request_id=request_id,
                path=self.path,
                method="POST",
                model=None,
                upstream=upstream_name,
                route_reason="official_image_generation",
                content_length=content_length,
                **request_context,
            )
            try:
                with _open_upstream_response(
                    request,
                    upstream_name=upstream_name,
                    upstream_format="images",
                    timeout=upstream_timeout_seconds(),
                    event_context=event_context,
                    request_kind=RETRY_REQUEST_MAIN_GENERATION,
                    max_attempts=1,
                    retry_http_errors=False,
                    transport_policy=TransportPolicy.OFFICIAL_KEEPALIVE,
                ) as response:
                    status = self._relay_raw_upstream_response(response, upstream_name)
            except HTTPError as exc:
                try:
                    status = self._relay_raw_upstream_response(exc, upstream_name)
                finally:
                    exc.close()
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=None,
                upstream=upstream_name,
                route_reason="official_image_generation",
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
        except GatewayUserRequestedShutdown:
            self._send_json_and_close(503, user_requested_shutdown_payload("responses"))
        except (IncompleteRead, OSError, URLError) as exc:
            detail = safe_upstream_error_detail(exc)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method="POST",
                model=None,
                upstream=upstream_name,
                route_reason="official_image_generation",
                status=502,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._send_json_and_close(
                502,
                {"error": type(exc).__name__, "detail": detail},
            )
        except Exception as exc:
            detail = safe_upstream_error_detail(exc)
            logger.error(
                "unexpected image generation proxy error request_id=%s detail=%s",
                request_id,
                detail,
            )
            self._send_json_and_close(
                500,
                {"error": type(exc).__name__, "detail": detail},
            )
        finally:
            _restore_gateway_request(previous_admission)
            shutdown_controller.complete(admission)

    def _proxy_post_request(self, *, inbound_format: str, provider_hint: str | None = None) -> None:
        """Shared POST handler for inbound Responses and Chat Completions requests.

        ``inbound_format`` is the wire format the *caller* used.  When it is
        ``chat_completions`` the request body is converted to Responses format
        before routing, and the upstream response is converted back to Chat
        Completions format before being returned to the caller.
        """
        request_id = uuid.uuid4().hex[:12]
        self._diagnostic_request_id = request_id
        self._pre_response_deadline = None
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        if not _local_request_authorized(self.headers, request_context):
            write_proxy_event(
                "request_error",
                request_id=request_id,
                path=self.path,
                method="POST",
                model=None,
                upstream="local",
                route_reason="local_client_auth",
                status=401,
                error="UnauthorizedLocalClient",
                detail="missing or invalid local Gateway client key",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._send_json(401, _local_gateway_auth_error_payload())
            self.close_connection = True
            return
        request_kind = RETRY_REQUEST_MAIN_GENERATION
        proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
        raw_provider_probe = raw_provider_probe_requested(self.headers, self.path)
        if raw_provider_probe:
            proxy_request_context["raw_provider_probe"] = True
        model = None
        model_requested = None
        upstream: Mapping[str, Any] | None = None
        upstream_name = None
        upstream_format = "responses"
        reports_cached_input_tokens = False
        behavior_profile = None
        route_reason: str | None = None
        route_plan: RoutePlan | None = None
        active_route_attempt: RouteAttemptPlan | None = None
        relay_execution_plan: RelayExecutionPlan | None = None
        route_policy_event_fields: dict[str, Any] = {}
        downstream_sse_started = False
        response_lifecycle_state: dict[str, str] = {}
        caller_body = b""
        caller_stream = True
        caller_request_observability: dict[str, Any] = {}
        request_observability: dict[str, Any] = {}
        usage_capture: dict[str, Any] = {}
        request_start_written = False
        write_request_start_once: Callable[[Mapping[str, Any]], None] | None = None

        def send_user_requested_shutdown() -> None:
            _record_user_requested_shutdown()
            if not self._send_user_requested_shutdown_outcome(
                inbound_format=inbound_format,
                downstream_sse_started=downstream_sse_started,
            ):
                finish_downstream_write_failure()

        def finish_downstream_write_failure(*, write_exc: OSError | None = None) -> None:
            exc = write_exc or OSError("downstream closed")
            failure_seam = _handler_downstream_stream_commit(self)
            if failure_seam is not None:
                failure_seam.close()
            self.close_connection = True
            write_proxy_event(
                "downstream_stream_closed",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name or "upstream_error",
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=499,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                **proxy_request_context,
            )
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                model_canonical=canonical_model_id(model) if model else None,
                upstream=upstream_name or "upstream_error",
                provider_id=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                reports_cached_input_tokens=reports_cached_input_tokens,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                is_stream=caller_stream,
                status=499,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_observability,
                **usage_capture,
                **proxy_request_context,
            )

        shutdown_controller = _gateway_shutdown_controller_for_handler(self)
        admission = shutdown_controller.admit()
        if admission is None:
            send_user_requested_shutdown()
            return
        previous_admission = _activate_gateway_request(admission)
        adapter_event_context: dict[str, Any] | None = None

        try:
            admission.raise_if_cancelled()
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 0:
                raise ValueError("Content-Length must be non-negative")
            max_body_bytes = max_request_body_bytes()
            if content_length > max_body_bytes:
                write_proxy_event(
                    "request_error",
                    request_id=request_id,
                    path=self.path,
                    method="POST",
                    model=None,
                    upstream="local",
                    route_reason="request_body_limit",
                    content_length=content_length,
                    max_request_body_bytes=max_body_bytes,
                    status=413,
                    error="RequestBodyTooLarge",
                    detail="request body exceeds configured limit",
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **proxy_request_context,
                )
                self._send_json(
                    413,
                    {
                        "error": "request body too large",
                        "max_request_body_bytes": max_body_bytes,
                    },
                )
                self.close_connection = True
                return
            request_input = _parse_gateway_request_input(
                self,
                inbound_format=inbound_format,
                provider_hint=provider_hint,
                request_id=request_id,
                started_at=started_at,
                request_context=request_context,
                proxy_request_context=proxy_request_context,
                raw_provider_probe=raw_provider_probe,
                content_length=content_length,
            )
            content_type = request_input.content_type
            content_encoding = request_input.content_encoding
            content_decoded = request_input.content_decoded
            decode_error = None
            body = request_input.body
            caller_body = request_input.body
            inbound_payload = request_input.inbound_payload
            request_kind = request_input.request_kind
            proxy_request_context = request_input.proxy_request_context
            model_requested = request_input.model_requested
            model = request_input.model
            route_reason = request_input.route_reason
            upstream = choose_upstream(model) if model else official_upstream()
            upstream_name = upstream["name"]
            upstream_format = str(upstream.get("upstream_format", "responses"))
            reports_cached_input_tokens = bool(upstream.get("reports_cached_input_tokens"))
            _validate_reasoning_effort_for_upstream(inbound_payload, upstream, model)
            inbound_has_image = (
                isinstance(inbound_payload, Mapping)
                and _value_contains_image(inbound_payload)
            )
            target_accepts_images = bool(
                model and model_supports_image(model, upstream)
            )
            image_proxy_enabled = gateway_image_proxy_enabled()
            caller_stream = (
                inbound_payload.get("stream") is True
                if isinstance(inbound_payload, Mapping)
                else True
            )
            route_runtime_facts: dict[str, RouteRuntimeFacts] = {
                request_kind: _route_runtime_facts(request_kind)
            }
            if request_kind != RETRY_REQUEST_MAIN_GENERATION:
                route_runtime_facts[RETRY_REQUEST_MAIN_GENERATION] = (
                    _route_runtime_facts(
                        RETRY_REQUEST_MAIN_GENERATION
                    )
                )
            route_plan = route_plan_for_request(
                upstream,
                request_context,
                inbound_format=inbound_format,
                provider_hint=provider_hint,
                model_requested=model_requested,
                canonical_route_model=model,
                request_kind=request_kind,
                raw_provider_probe=raw_provider_probe,
                input_has_image=inbound_has_image,
                target_accepts_images=target_accepts_images,
                image_proxy_enabled=image_proxy_enabled,
                official_http_passthrough_enabled=(
                    gateway_official_http_passthrough_enabled()
                ),
                caller_stream=caller_stream,
                runtime_facts=route_runtime_facts,
            )
            primary_route_attempt = route_plan.primary_attempt
            behavior_profile = route_plan.behavior_profile
            upstream_format = route_plan.selected_upstream_format
            if request_kind != route_plan.request_kind:
                request_kind = route_plan.request_kind
                proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
            self._pre_response_deadline = (
                primary_route_attempt.retry.pre_response_deadline(started_at)
                if primary_route_attempt is not None
                else None
            )
            reasoning_policy = _reasoning_policy_for_request(inbound_payload, upstream, model)
            route_policy_event_fields = {
                **_route_plan_event_fields(route_plan),
                **(
                    _route_attempt_event_fields(
                        primary_route_attempt,
                        provider_id=route_plan.provider_id,
                        model_requested=route_plan.model_requested,
                        model_canonical=route_plan.canonical_model,
                        upstream_model=route_plan.upstream_model,
                    )
                    if primary_route_attempt is not None
                    else {}
                ),
                **({"reasoning_policy": reasoning_policy} if reasoning_policy else {}),
            }
            proxy_request_context = {
                **proxy_request_context,
                **route_policy_event_fields,
            }
            if primary_route_attempt is None:
                protocol_error = (
                    UnsupportedRouteProtocolError
                    if (
                        route_plan.protocol_capability_state
                        == CapabilityState.UNSUPPORTED
                    )
                    else UnqualifiedRouteProtocolError
                )
                raise protocol_error(
                    route_plan.protocol_failure_reason
                    or "configured upstream protocol is not executable"
                )
            if route_plan.vision.action == VisionAction.REJECT:
                model_label = canonical_model_id(model) if model else "the target model"
                raise ImageProxyError(
                    f"{model_label} does not support image input and Vision Proxy is disabled."
                )
            operational_authentication = (
                materialize_operational_authentication(
                    self.headers,
                    upstream,
                )
            )
            route_plan = bind_route_plan_operational_authentication(
                route_plan,
                self.headers,
                upstream,
                operational_authentication,
                drop_content_encoding=content_decoded,
            )
            primary_route_attempt = route_plan.attempts[0]
            model_canonical = canonical_model_id(model) if model else None
            # Create the request-scoped downstream stream-commit seam early so that
            # every production SSE header/body (status, retry, keepalive, converted
            # output, error, terminal) is authorized by the same lifecycle owner.
            # The upstream response is attached once it is opened.
            self._downstream_stream_commit = _GatewayDownstreamStreamCommit(
                self,
                None,
                upstream_name or "unknown",
                model=model_canonical,
                request_id=request_id,
                inbound_format=inbound_format,
                upstream_format=upstream_format,
            )
            if (
                route_plan.tool_exposure.strip_caller_tools
                and isinstance(inbound_payload, dict)
                and _strip_tools_for_compact_payload(
                    inbound_payload,
                    event_context={
                        "request_id": request_id,
                        "behavior_profile": behavior_profile,
                        **proxy_request_context,
                    },
                )
            ):
                body = json.dumps(inbound_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            # Capture the caller's desired stream mode and prompt cache key
            # before compatibility helpers can force stream=true or reshape the
            # body for the selected upstream.
            prompt_cache_key = None
            if isinstance(inbound_payload, Mapping) and isinstance(inbound_payload.get("prompt_cache_key"), str):
                prompt_cache_key = inbound_payload["prompt_cache_key"]
            caller_request_observability = proxy_telemetry.enrich_request_observability(
                body=caller_body,
                codex_home=RUNTIME_CODEX_DIR,
                upstream=upstream,
                include_body_hmac=(
                    route_plan.request_mutation_policy
                    != MutationPolicy.OFFICIAL_PASSTHROUGH
                ),
                prompt_cache_key=prompt_cache_key,
                extract_prompt_cache_key=(
                    route_plan.request_mutation_policy
                    != MutationPolicy.OFFICIAL_PASSTHROUGH
                ),
            )

            def emit_request_start_once(observability_fields: Mapping[str, Any]) -> None:
                nonlocal request_start_written
                if request_start_written:
                    return
                write_proxy_event(
                    "request_start",
                    request_id=request_id,
                    path=self.path,
                    method="POST",
                    model=model_canonical,
                    model_requested=model_requested,
                    model_canonical=model_canonical,
                    upstream=upstream_name,
                    provider_id=upstream_name,
                    provider_hint=provider_hint,
                    upstream_format=upstream_format,
                    reports_cached_input_tokens=reports_cached_input_tokens,
                    behavior_profile=behavior_profile,
                    route_reason=route_reason,
                    route_mode="official" if upstream_name == "official" else "codexhub",
                    inbound_format=inbound_format,
                    is_stream=caller_stream,
                    content_length=content_length,
                    decoded_content_length=len(caller_body) if content_decoded else None,
                    content_type=content_type[:120] if content_type else None,
                    content_encoding=content_encoding[:80] if content_encoding else None,
                    content_decoded=content_decoded,
                    decode_error=decode_error[:160] if decode_error else None,
                    **dict(observability_fields),
                    **proxy_request_context,
                )
                request_start_written = True

            write_request_start_once = emit_request_start_once
            # Convert inbound Chat Completions request to Responses format before routing
            # only for Gateway compatibility paths. Same-format transparent traffic
            # must stay in the caller's wire format.
            if (
                route_plan.caller_request_body_mode
                == CallerRequestBodyMode.CONVERT_CHAT_TO_RESPONSES
            ):
                body = _chat_completions_request_to_responses_body(body)
            adapter_event_context = {
                "request_id": request_id,
                "model": model_canonical,
                "behavior_profile": behavior_profile,
                **proxy_request_context,
                "_caller_wire_format": inbound_format,
            }

            def emit_downstream_status(status_payload: Mapping[str, Any]) -> bool:
                nonlocal downstream_sse_started
                if not caller_stream:
                    return True
                if not downstream_sse_started:
                    if not self._send_sse_headers(200, upstream_name):
                        return False
                    downstream_sse_started = True
                return self._write_sse_data(
                    _downstream_stream_status_payload(inbound_format, status_payload, model_canonical)
                )

            usage_capture: dict[str, Any] = {}
            if (
                route_plan.request_mutation_policy
                == MutationPolicy.OFFICIAL_PASSTHROUGH
            ):
                body = official_passthrough_request_body(
                    body,
                    inbound_payload,
                    upstream,
                    model_id=model,
                )
            elif (
                route_plan.request_mutation_policy
                == MutationPolicy.TRANSPARENT
            ):
                body = transparent_request_body(
                    body,
                    _safe_json_mapping(body),
                    upstream,
                    model_id=model,
                )
                body, developer_role_rewrites = _rewrite_transparent_developer_role_messages(body, upstream)
                if developer_role_rewrites:
                    write_proxy_event(
                        "developer_role_rewrite_applied",
                        request_id=request_id,
                        model=model_canonical,
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        messages_rewritten=developer_role_rewrites,
                        **proxy_request_context,
                    )
                # Unconditional by design: rewriting boolean JSON Schemas to
                # their equivalent object forms is semantics-preserving for
                # every upstream, and intolerant validators (e.g. Moonshot)
                # fail closed without it. No provider capability gate.
                body, tool_schema_rewrites = _normalize_transparent_tool_schema_booleans(body)
                if tool_schema_rewrites:
                    write_proxy_event(
                        "tool_schema_boolean_normalized",
                        request_id=request_id,
                        model=model_canonical,
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        schemas_rewritten=tool_schema_rewrites,
                        **proxy_request_context,
                    )
                if route_plan.transparent_tool_loop_guard:
                    transparent_payload = _safe_json_mapping(body) or {}
                    repeated_count = (
                        _excessive_transparent_responses_tool_loop_count(transparent_payload)
                        if inbound_format == "responses"
                        else _excessive_transparent_chat_tool_loop_count(transparent_payload)
                        if inbound_format == "chat_completions"
                        else None
                    )
                    if repeated_count is not None:
                        raise UpstreamProtocolTranslationError(
                            UnsupportedProtocolTranslationError(
                                EXCESSIVE_TOOL_LOOP_ERROR_CODE,
                                f"Repeated successful function calls exceeded the bound of {EXCESSIVE_TOOL_LOOP_BOUND}.",
                            )
                        )
            else:
                compatibility_upstream = {
                    **upstream,
                    "upstream_format": primary_route_attempt.selected_upstream_format,
                }
                body = compatible_request_body(
                    body,
                    compatibility_upstream,
                    model_id=model,
                    event_context=adapter_event_context,
                    inject_codex_tools=route_plan.tool_exposure.gateway_schema_injection,
                    tool_protocol_override=(
                        primary_route_attempt.tool_protocol
                    ),
                    tool_surface_strategy_override=(
                        primary_route_attempt.tool_surface_strategy
                    ),
                    native_responses_tool_codec_override=(
                        primary_route_attempt.native_responses_tool_codec
                    ),
                )
            vision_proxy_payload_format = (
                route_plan.prepared_request_protocol.value
            )
            image_proxy_payload: dict[str, Any] | None = None
            if route_plan.vision.action == VisionAction.PROXY:
                try:
                    parsed_image_proxy_payload = json.loads(body.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_image_proxy_payload = None
                if isinstance(parsed_image_proxy_payload, dict):
                    image_proxy_payload = parsed_image_proxy_payload
            try:
                if route_plan.vision.action == VisionAction.PROXY:
                    if image_proxy_payload is None:
                        raise ImageProxyError(
                            "Vision Proxy could not inspect the planned image payload."
                        )
                    image_proxy_changed = enforce_text_only_image_boundary(
                        image_proxy_payload,
                        inbound_format=vision_proxy_payload_format,
                        target_model=model,
                        target_upstream=upstream,
                        vision_plan=route_plan.vision,
                        event_context=adapter_event_context,
                        progress_callback=emit_downstream_status if caller_stream else None,
                    )
                    if image_proxy_changed:
                        body = json.dumps(image_proxy_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            except DownstreamClosedDuringImageProxyError:
                finish_downstream_write_failure()
                return
            seam = _handler_downstream_stream_commit(self)
            if seam is not None and seam.downstream_closed:
                finish_downstream_write_failure(
                    write_exc=seam.last_write_error() or OSError("downstream closed")
                )
                return
            responses_body = body
            def upstream_body_for_attempt(
                attempt: RouteAttemptPlan,
                prepared_body: bytes = responses_body,
            ) -> bytes:
                return attempt.request_body(prepared_body)

            def request_observability_for_attempt(
                attempt: RouteAttemptPlan,
                attempt_body: bytes,
            ) -> dict[str, Any]:
                upstream_observability = (
                    proxy_telemetry.enrich_request_observability(
                        body=attempt_body,
                        codex_home=RUNTIME_CODEX_DIR,
                        upstream=upstream,
                        include_body_hmac=(
                            attempt.request_mutation_policy
                            != MutationPolicy.OFFICIAL_PASSTHROUGH
                        ),
                        prompt_cache_key=prompt_cache_key,
                        extract_prompt_cache_key=(
                            attempt.request_mutation_policy
                            != MutationPolicy.OFFICIAL_PASSTHROUGH
                        ),
                    )
                )
                return {
                    **upstream_observability,
                    **_request_observability_with_prefix(
                        caller_request_observability,
                        "caller",
                    ),
                    **_request_observability_with_prefix(
                        upstream_observability,
                        "upstream",
                    ),
                    "request_observability_scope": "executed_attempt",
                    "request_observability_attempt_index": attempt.index,
                    "request_observability_upstream_protocol": (
                        attempt.upstream_protocol.value
                    ),
                }

            request_observability = request_observability_for_attempt(
                primary_route_attempt,
                upstream_body_for_attempt(primary_route_attempt),
            )
            emit_request_start_once(request_observability)
            emit_retry_to_downstream = (
                primary_route_attempt.retry.emit_downstream_retry_notice
            )

            def upstream_request_for_attempt(
                attempt: RouteAttemptPlan,
                lifecycle_final_retry_reason: str | None = None,
            ) -> tuple[Request, dict[str, Any]]:
                request_body = responses_body
                if lifecycle_final_retry_reason:
                    request_body = _responses_body_with_lifecycle_final_retry_guidance(
                        responses_body,
                        lifecycle_final_retry_reason,
                    )
                    _write_adapter_event(
                        adapter_event_context,
                        "lifecycle_final_retry_guidance_injected",
                        upstream=upstream_name,
                        upstream_format=attempt.selected_upstream_format,
                        reason=lifecycle_final_retry_reason,
                    )
                attempt_body = upstream_body_for_attempt(
                    attempt,
                    request_body,
                )
                return (
                    Request(
                        attempt.endpoint_url,
                        data=attempt_body,
                        headers=attempt.request_headers.to_dict(),
                        method="POST",
                    ),
                    request_observability_for_attempt(
                        attempt,
                        attempt_body,
                    ),
                )

            def emit_downstream_retry(payload: Mapping[str, Any]) -> bool:
                nonlocal downstream_sse_started
                if not emit_retry_to_downstream:
                    return True
                if not downstream_sse_started:
                    if not self._send_sse_headers(200, upstream_name):
                        return False
                    downstream_sse_started = True
                if not self._write_sse_event("codexhub.retry", payload):
                    return False
                notice_fields = dict(proxy_request_context)
                notice_fields.update(
                    {
                        "request_id": request_id,
                        "model": model_canonical,
                        "model_requested": model_requested,
                        "model_canonical": model_canonical,
                        "upstream": upstream_name,
                        "provider_id": upstream_name,
                        "upstream_format": upstream_format,
                        "behavior_profile": behavior_profile,
                        "route_reason": route_reason,
                        "route_mode": "official" if upstream_name == "official" else "codexhub",
                        "inbound_format": inbound_format,
                        "is_stream": caller_stream,
                    }
                )
                retry_payload = dict(payload)
                retry_payload.pop("type", None)
                notice_fields.update(retry_payload)
                write_proxy_event("sse_retry_notice", **notice_fields)
                return True

            def mark_downstream_sse_started() -> None:
                nonlocal downstream_sse_started
                downstream_sse_started = True

            open_attempt_budget = (
                primary_route_attempt.retry.new_open_attempt_budget()
            )
            runtime_tool_compatibility_attempt_generation = 0
            for route_attempt in route_plan.attempts:
                active_route_attempt = route_attempt
                upstream_format = route_attempt.selected_upstream_format
                route_attempt_event_fields = _route_attempt_event_fields(
                    route_attempt,
                    provider_id=route_plan.provider_id,
                    model_requested=route_plan.model_requested,
                    model_canonical=route_plan.canonical_model,
                    upstream_model=route_plan.upstream_model,
                )
                proxy_request_context.update(route_attempt_event_fields)
                if isinstance(adapter_event_context, dict):
                    adapter_event_context.update(route_attempt_event_fields)
                    adapter_event_context["tool_protocol"] = (
                        route_attempt.tool_protocol
                    )
                base_relay_attempts = route_attempt.retry.base_relay_attempts
                relay_attempts = base_relay_attempts
                lifecycle_final_extra_attempts = (
                    route_attempt.retry.lifecycle_final_extra_attempts(
                        adapter_event_context
                    )
                )
                relay_execution_plan = route_attempt.relay_execution_plan(
                    lifecycle_final_retry_enabled=(
                        lifecycle_final_extra_attempts > 0
                    )
                )
                max_relay_attempts = relay_attempts + lifecycle_final_extra_attempts
                relay_attempt = 1
                lifecycle_final_retry_reason: str | None = None
                try:
                    while relay_attempt <= max_relay_attempts:
                        runtime_tool_compatibility_attempt_generation += 1
                        if isinstance(adapter_event_context, dict):
                            adapter_event_context[
                                _RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY
                            ] = runtime_tool_compatibility_attempt_generation
                        seam = _handler_downstream_stream_commit(self)
                        if seam is not None:
                            seam.set_upstream_format(upstream_format)
                        request, request_observability = upstream_request_for_attempt(
                            route_attempt,
                            lifecycle_final_retry_reason,
                        )
                        emit_request_start_once(request_observability)
                        try:
                            with _open_upstream_response(
                                request,
                                upstream_name=upstream_name,
                                upstream_format=upstream_format,
                                timeout=(
                                    route_attempt.retry.request_timeout_seconds
                                ),
                                event_context=adapter_event_context,
                                downstream_retry_callback=emit_downstream_retry if emit_retry_to_downstream else None,
                                retry_execution=route_attempt.retry,
                                transport_policy=route_attempt.transport_policy,
                                downstream_exposed=lambda: _downstream_has_been_exposed(self),
                                pre_response_deadline=(
                                    None if downstream_sse_started else self._pre_response_deadline
                                ),
                                open_attempt_budget=open_attempt_budget,
                            ) as response:
                                seam = _handler_downstream_stream_commit(self)
                                if seam is not None:
                                    seam.attach_upstream_response(response)
                                status = self._relay_upstream_response(
                                    response,
                                    upstream_name,
                                    request_id=request_id,
                                    model=model_canonical,
                                    inbound_format=inbound_format,
                                    caller_stream=caller_stream,
                                    event_context=adapter_event_context,
                                    usage_capture=usage_capture,
                                    headers_already_sent=downstream_sse_started,
                                    defer_stream_errors=relay_attempt < relay_attempts,
                                    mark_downstream_sse_started=mark_downstream_sse_started,
                                    response_lifecycle_state=response_lifecycle_state,
                                    relay_execution_plan=relay_execution_plan,
                                )
                            break
                        except DownstreamClosedBeforeRetryError:
                            finish_downstream_write_failure()
                            return
                        except (
                            CompactEmptyResponseError,
                            IncompleteRead,
                            UpstreamStreamInterruptedError,
                            UpstreamStreamIdleTimeoutError,
                            UpstreamStreamIncompleteError,
                            UpstreamStreamErrorEvent,
                            LifecycleEmptyFinalResponseError,
                            LifecycleFinalFormatResponseError,
                        ) as exc:
                            active_request = _active_gateway_request()
                            if active_request is not None:
                                active_request.raise_if_cancelled()
                            lifecycle_retry = isinstance(
                                exc,
                                (LifecycleEmptyFinalResponseError, LifecycleFinalFormatResponseError),
                            )
                            retry_safety_class: str | None = None
                            if lifecycle_retry:
                                stream_failure = True
                                retry_exc: BaseException = exc
                                failure_class = RETRY_FAILURE_QUICK_TRANSIENT
                                lifecycle_final_retry_reason = "empty" if isinstance(exc, LifecycleEmptyFinalResponseError) else "format"
                                stream_model_access_path = _model_access_path_from_event_context(
                                    adapter_event_context,
                                    upstream_name,
                                    upstream_format,
                                )
                                retry_safety_class = _retry_safety_class(
                                    retry_exc,
                                    request=request,
                                    upstream_name=upstream_name,
                                    request_kind=request_kind,
                                    downstream_exposed=_downstream_has_been_exposed(self),
                                    model_access_path=stream_model_access_path,
                                    failure_phase="stream_body",
                                )
                                if retry_safety_class in _SUPPRESSED_RETRY_SAFETY_CLASSES:
                                    _emit_upstream_retry_suppressed_event(
                                        adapter_event_context,
                                        upstream_name=upstream_name,
                                        upstream_format=upstream_format,
                                        request_kind=request_kind,
                                        attempt=relay_attempt,
                                        max_attempts=max_relay_attempts,
                                        exc=retry_exc,
                                        failure_class=failure_class,
                                        failure_phase="stream_body",
                                        retry_safety_class=retry_safety_class,
                                    )
                                    raise retry_exc
                                retry_limit = max_relay_attempts
                                if relay_attempt >= retry_limit:
                                    raise
                                delay_seconds = 0
                            else:
                                stream_failure = isinstance(
                                    exc,
                                    (
                                        UpstreamStreamInterruptedError,
                                        UpstreamStreamIdleTimeoutError,
                                        UpstreamStreamIncompleteError,
                                    ),
                                )
                                is_stream_error_event = isinstance(exc, UpstreamStreamErrorEvent)
                                retry_exc = exc.cause if isinstance(exc, UpstreamStreamInterruptedError) else exc
                                failure_class = _upstream_failure_class(retry_exc)
                                relay_attempts = route_attempt.retry.relay_attempts_for_failure_class(
                                    failure_class=failure_class,
                                    stream_failure=stream_failure,
                                )
                                if isinstance(retry_exc, UpstreamEmptyCompletedResponseError):
                                    relay_attempts = min(
                                        relay_attempts,
                                        route_attempt.retry.empty_completed_max_attempts,
                                    )
                                max_relay_attempts = relay_attempts + lifecycle_final_extra_attempts
                                retry_limit = relay_attempts
                                stream_failure_phase = "stream_body" if (stream_failure or is_stream_error_event) else None
                                stream_model_access_path = _model_access_path_from_event_context(
                                    adapter_event_context,
                                    upstream_name,
                                    upstream_format,
                                )
                                retry_safety_class = _retry_safety_class(
                                    retry_exc,
                                    request=request,
                                    upstream_name=upstream_name,
                                    request_kind=request_kind,
                                    downstream_exposed=_downstream_has_been_exposed(self),
                                    model_access_path=stream_model_access_path,
                                    failure_phase=stream_failure_phase,
                                )
                                if retry_safety_class in _SUPPRESSED_RETRY_SAFETY_CLASSES:
                                    _emit_upstream_retry_suppressed_event(
                                        adapter_event_context,
                                        upstream_name=upstream_name,
                                        upstream_format=upstream_format,
                                        request_kind=request_kind,
                                        attempt=relay_attempt,
                                        max_attempts=retry_limit,
                                        exc=retry_exc,
                                        failure_class=failure_class,
                                        failure_phase=stream_failure_phase,
                                        retry_safety_class=retry_safety_class,
                                    )
                                    if isinstance(retry_exc, UpstreamEmptyCompletedResponseError):
                                        detail = "Upstream Responses stream completed without visible output or tool calls."
                                        write_proxy_event(
                                            "upstream_empty_completed_response",
                                            request_id=request_id,
                                            model=model,
                                            upstream=upstream_name,
                                            status=502,
                                            upstream_format=upstream_format,
                                            inbound_format=inbound_format,
                                            terminal_seen=False,
                                            completed_seen=True,
                                            visible_or_tool_output_seen=False,
                                            completed_tool_calls=0,
                                            pending_downstream_lines=0,
                                            pending_downstream_bytes=0,
                                            last_event_type="response.completed",
                                        )
                                        if not self._write_downstream_sse_error(
                                            inbound_format=inbound_format,
                                            upstream_name=upstream_name,
                                            status=502,
                                            error="upstream_empty_completed_response",
                                            detail=detail,
                                            redact_identity=_retry_identity_from_context(adapter_event_context),
                                        ):
                                            finish_downstream_write_failure()
                                            return
                                        _capture_usage(usage_capture, None, missing_reason="empty_completed_response")
                                        return
                                    raise retry_exc
                                if relay_attempt >= retry_limit or failure_class == RETRY_FAILURE_PERMANENT:
                                    raise retry_exc
                                delay_seconds = route_attempt.retry.retry_delay_seconds(
                                    relay_attempt,
                                    failure_class=failure_class,
                                    exc=retry_exc,
                                )
                                retry_elapsed_seconds = max(
                                    0.0,
                                    time.monotonic() - started_at,
                                )
                                if (
                                    failure_class
                                    in CAPACITY_RETRY_FAILURE_CLASSES
                                    and not route_attempt.retry.capacity_elapsed_limit_allows(
                                        retry_elapsed_seconds,
                                        delay_seconds,
                                    )
                                ):
                                    raise retry_exc
                                if (
                                    stream_failure
                                    and failure_class == RETRY_FAILURE_QUICK_TRANSIENT
                                    and not route_attempt.retry.stream_elapsed_limit_allows(
                                        retry_elapsed_seconds,
                                        delay_seconds,
                                    )
                                ):
                                    raise retry_exc
                            _emit_upstream_retry_event(
                                adapter_event_context,
                                upstream_name=upstream_name,
                                upstream_format=upstream_format,
                                request_kind=request_kind,
                                attempt=relay_attempt,
                                max_attempts=retry_limit,
                                exc=retry_exc,
                                delay_seconds=delay_seconds,
                                failure_class=failure_class,
                                failure_phase="stream_body" if stream_failure else None,
                                retry_safety_class=retry_safety_class,
                            )
                            if not emit_downstream_retry(
                                _downstream_retry_payload(
                                    upstream_name=upstream_name,
                                    upstream_format=upstream_format,
                                    request_kind=request_kind,
                                    attempt=relay_attempt,
                                    max_attempts=retry_limit,
                                    exc=retry_exc,
                                    delay_seconds=delay_seconds,
                                    failure_class=failure_class,
                                    failure_phase="stream_body" if stream_failure else None,
                                    redact_identity=_retry_identity_from_context(adapter_event_context),
                                )
                            ):
                                finish_downstream_write_failure()
                                return
                            _sleep_for_retry_with_gateway_cancellation(delay_seconds)
                            relay_attempt += 1
                            continue
                        relay_attempt += 1
                    else:
                        raise RuntimeError("unreachable upstream relay retry state")
                    break
                except HTTPError as exc:
                    next_attempt_index = route_attempt.index + 1
                    next_attempt = (
                        route_plan.attempts[next_attempt_index]
                        if next_attempt_index < len(route_plan.attempts)
                        else None
                    )
                    fallback_allowed = (
                        next_attempt is not None
                        and not downstream_sse_started
                        and route_attempt.allows_protocol_fallback_status(
                            getattr(exc, "code", None)
                        )
                    )
                    if fallback_allowed:
                        fallback_model_access_path = _model_access_path_from_event_context(
                            adapter_event_context,
                            upstream_name,
                            upstream_format,
                        )
                        fallback_retry_safety_class = _retry_safety_class(
                            exc,
                            request=request,
                            upstream_name=upstream_name,
                            request_kind=request_kind,
                            downstream_exposed=bool(downstream_sse_started),
                            model_access_path=fallback_model_access_path,
                            failure_phase="response_headers",
                        )
                        if fallback_retry_safety_class not in _SUPPRESSED_RETRY_SAFETY_CLASSES:
                            failed_attempt_snapshot = (
                                route_attempt.telemetry_snapshot()
                            )
                            next_attempt_snapshot = (
                                next_attempt.telemetry_snapshot()
                            )
                            write_proxy_event(
                                "upstream_protocol_fallback",
                                request_id=request_id,
                                model=model_canonical,
                                model_requested=model_requested,
                                model_canonical=model_canonical,
                                upstream=upstream_name,
                                provider_id=upstream_name,
                                provider_hint=provider_hint,
                                upstream_format=route_plan.configured_upstream_protocol_name,
                                behavior_profile=behavior_profile,
                                failed_upstream_format=upstream_format,
                                next_upstream_format=next_attempt.selected_upstream_format,
                                failed_route_attempt_index=(
                                    failed_attempt_snapshot["index"]
                                ),
                                failed_route_attempt_request_body_mode=(
                                    failed_attempt_snapshot[
                                        "request_body_mode"
                                    ]
                                ),
                                failed_route_attempt_request_conversion_steps=(
                                    failed_attempt_snapshot[
                                        "request_conversion_steps"
                                    ]
                                ),
                                failed_route_attempt_mutation_summary=(
                                    failed_attempt_snapshot[
                                        "mutation_summary"
                                    ]
                                ),
                                next_route_attempt_index=(
                                    next_attempt_snapshot["index"]
                                ),
                                next_route_attempt_request_body_mode=(
                                    next_attempt_snapshot[
                                        "request_body_mode"
                                    ]
                                ),
                                next_route_attempt_request_conversion_steps=(
                                    next_attempt_snapshot[
                                        "request_conversion_steps"
                                    ]
                                ),
                                next_route_attempt_mutation_summary=(
                                    next_attempt_snapshot[
                                        "mutation_summary"
                                    ]
                                ),
                                status=getattr(exc, "code", 502),
                                error="HTTPError",
                                detail=safe_upstream_error_detail(
                                    exc,
                                    redact_identity=_retry_identity_from_context(adapter_event_context),
                                ),
                                **request_observability,
                                **proxy_request_context,
                            )
                            continue
                        _emit_upstream_retry_suppressed_event(
                            adapter_event_context,
                            upstream_name=upstream_name,
                            upstream_format=upstream_format,
                            request_kind=request_kind,
                            attempt=relay_attempt,
                            max_attempts=relay_attempts,
                            exc=exc,
                            failure_class=_upstream_failure_class(exc),
                            failure_phase="response_headers",
                            retry_safety_class=fallback_retry_safety_class,
                        )
                    raise
            else:
                raise RuntimeError("unreachable upstream protocol selection state")
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=model_canonical,
                model_requested=model_requested,
                model_canonical=model_canonical,
                upstream=upstream_name,
                provider_id=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                reports_cached_input_tokens=reports_cached_input_tokens,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                is_stream=caller_stream,
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_observability,
                **usage_capture,
                **proxy_request_context,
            )
        except GatewayUserRequestedShutdown:
            send_user_requested_shutdown()
        except CompactEmptyResponseError as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = safe_upstream_error_detail(exc, redact_identity=identity)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=502,
                error="compact_empty_response",
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=502,
                    exc=exc,
                    error="compact_empty_response",
                    detail=detail,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                error="compact_empty_response",
                detail=detail,
                redact_identity=identity,
                error_type="compact_empty_response",
            )
        except (LifecycleEmptyFinalResponseError, LifecycleFinalFormatResponseError) as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = safe_upstream_error_detail(exc, redact_identity=identity)
            error_code = (
                "lifecycle_empty_final_response"
                if isinstance(exc, LifecycleEmptyFinalResponseError)
                else "lifecycle_final_format_response"
            )
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                status=502,
                error=error_code,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=502,
                    exc=exc,
                    error=error_code,
                    detail=detail,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                error=error_code,
                detail=detail,
                redact_identity=identity,
                error_type=error_code,
            )
        except ModelIdentityResolutionError as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            error_code = "model_identity_error"
            identity = _retry_identity_from_context(adapter_event_context)
            detail = safe_upstream_error_detail(exc, redact_identity=identity)
            detail = _redact_identity_in_text(detail, exc.model_slug)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name or "gateway",
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=400,
                error=error_code,
                detail=detail,
                identity_classification=exc.classification,
                identity_reason=exc.reason,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "gateway",
                    status=400,
                    exc=exc,
                    error=error_code,
                    detail=detail,
                    error_type=error_code,
                    redact_identity=identity,
                    preserve_explicit_error=True,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                400,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "gateway",
                request_id=request_id,
                exc=exc,
                error=error_code,
                detail=detail,
                error_type=error_code,
                redact_identity=identity,
            )
        except ImageProxyError as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = _redact_identity_in_text(str(exc)[:300], identity)
            if not request_start_written and callable(write_request_start_once):
                fallback_request_observability = {
                    **caller_request_observability,
                    **_request_observability_with_prefix(caller_request_observability, "caller"),
                }
                write_request_start_once(fallback_request_observability)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=502,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=502,
                    exc=exc,
                    error="image_proxy_error",
                    detail=detail,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                error="image_proxy_error",
                detail=detail,
                redact_identity=identity,
                error_type="image_proxy_error",
            )
        except UpstreamProtocolTranslationError as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = _redact_identity_in_text(str(exc), identity)
            error_code = exc.cause.code
            is_apply_patch_adapter_error = error_code == APPLY_PATCH_ADAPTER_ERROR_CODE
            error_status = 400
            json_error_type = "invalid_request_error" if is_apply_patch_adapter_error else error_code
            sse_error_type = "invalid_request_error" if is_apply_patch_adapter_error else "upstream_error"
            selected_native_responses_tool_codec = (
                active_route_attempt.native_responses_tool_codec
                if active_route_attempt is not None
                else "none"
            )
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=error_status,
                error=error_code,
                detail=detail[:300],
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if inbound_format == "responses":
                    if not self._write_sse_event(
                        "response.failed",
                        _responses_failed_event_for_stream_error(
                            upstream_name=upstream_name or "upstream_error",
                            model=canonical_model_id(model) if model else None,
                            status=error_status,
                            exc=exc,
                            error=error_code,
                            detail=detail,
                            response_id=response_lifecycle_state.get("response_id"),
                            redact_identity=identity,
                        ),
                    ):
                        finish_downstream_write_failure()
                        return
                    if (
                        is_apply_patch_adapter_error
                        and selected_native_responses_tool_codec == "strict_apply_patch"
                    ):
                        write_proxy_event(
                            "third_party_apply_patch_terminal",
                            request_id=request_id,
                            model=canonical_model_id(model) if model else None,
                            upstream=upstream_name,
                            codec=selected_native_responses_tool_codec,
                            disposition="response.failed",
                            failure_class=RETRY_FAILURE_PERMANENT,
                            retry_count=0,
                        )
                    self.close_connection = True
                else:
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name or "upstream_error",
                        status=error_status,
                        exc=exc,
                        error=error_code,
                        detail=detail,
                        error_type=sse_error_type,
                        preserve_explicit_error=True,
                        redact_identity=identity,
                    ):
                        finish_downstream_write_failure()
                        return
                return
            self._safe_send_downstream_json_error(
                error_status,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                error=error_code,
                detail=detail,
                redact_identity=identity,
                error_type=json_error_type,
            )
        except ValueError as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = _redact_identity_in_text(str(exc)[:300], identity)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=400,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            self._safe_send_downstream_json_error(
                400,
                inbound_format=inbound_format,
                upstream_name=upstream_name or provider_hint or "gateway",
                request_id=request_id,
                exc=exc,
                error=type(exc).__name__,
                detail=detail,
                redact_identity=identity,
                error_type="invalid_request_error",
            )
        except HTTPError as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=getattr(exc, "code", 502),
                    exc=exc,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                    return
                write_proxy_event(
                    "request_complete",
                    request_id=request_id,
                    method="POST",
                    model=canonical_model_id(model) if model else None,
                    model_requested=model_requested,
                    model_canonical=canonical_model_id(model) if model else None,
                    upstream=upstream_name,
                    provider_id=upstream_name,
                    provider_hint=provider_hint,
                    upstream_format=upstream_format,
                    reports_cached_input_tokens=reports_cached_input_tokens,
                    behavior_profile=behavior_profile,
                    inbound_format=inbound_format,
                    route_reason=route_reason,
                    route_mode="official" if upstream_name == "official" else "codexhub",
                    is_stream=caller_stream,
                    status=getattr(exc, "code", 502),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **request_observability,
                    **usage_capture,
                    **proxy_request_context,
                )
                return
            try:
                if relay_execution_plan is None:
                    raise RuntimeError(
                        "upstream response arrived without a planned relay "
                        "execution contract"
                    ) from exc
                previous_retry_identity = (
                    adapter_event_context.get("_retry_attempt_identity")
                    if isinstance(adapter_event_context, Mapping)
                    else None
                )
                adapter_event_context = {
                    "request_id": request_id,
                    "model": canonical_model_id(model) if model else None,
                    "behavior_profile": behavior_profile,
                    **proxy_request_context,
                }
                if isinstance(previous_retry_identity, str) and previous_retry_identity:
                    adapter_event_context["_retry_attempt_identity"] = previous_retry_identity
                status = self._relay_upstream_response(
                    exc,
                    upstream_name or "upstream_error",
                    request_id=request_id,
                    model=canonical_model_id(model) if model else None,
                    inbound_format=inbound_format,
                    caller_stream=caller_stream,
                    event_context=adapter_event_context,
                    usage_capture=usage_capture,
                    relay_execution_plan=relay_execution_plan,
                )
            except OSError as relay_exc:
                self.close_connection = True
                write_proxy_event(
                    "client_write_failed",
                    request_id=request_id,
                    model=canonical_model_id(model) if model else None,
                    upstream=upstream_name,
                    upstream_format=upstream_format,
                    behavior_profile=behavior_profile,
                    status=getattr(exc, "code", 502),
                    error=type(relay_exc).__name__,
                    detail=safe_upstream_error_detail(relay_exc),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **proxy_request_context,
                )
                return
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                model_canonical=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                provider_id=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                reports_cached_input_tokens=reports_cached_input_tokens,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                is_stream=caller_stream,
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_observability,
                **usage_capture,
                **proxy_request_context,
            )
        except GatewayPreResponseBudgetExhausted as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            error_code = "gateway_pre_response_budget_exhausted"
            detail = "Gateway pre-response budget exhausted before a usable upstream response."
            event_fields = {
                "failure_phase": exc.phase,
                "attempt": exc.attempt,
                "pre_response_budget_ms": int(exc.budget_seconds * 1000),
                "retryable": False,
            }
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=504,
                error=error_code,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **event_fields,
                **proxy_request_context,
            )
            self._safe_send_downstream_json_error(
                504,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "gateway",
                request_id=request_id,
                error=error_code,
                detail=detail,
                error_type=error_code,
            )
            self.close_connection = True
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                model_canonical=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                provider_id=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                reports_cached_input_tokens=reports_cached_input_tokens,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                is_stream=caller_stream,
                status=504,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **event_fields,
                **request_observability,
                **usage_capture,
                **proxy_request_context,
            )
        except IncompleteRead as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = safe_upstream_error_detail(exc, redact_identity=identity)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                status=502,
                error=type(exc).__name__,
                detail=detail,
                failure_phase=transport_failure_phase(exc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    exc=exc,
                    detail=detail,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
                redact_identity=identity,
            )
        except (OSError, URLError) as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = safe_upstream_error_detail(exc, redact_identity=identity)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                status=502,
                error=type(exc).__name__,
                detail=detail,
                failure_phase=transport_failure_phase(exc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    exc=exc,
                    detail=detail,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
                redact_identity=identity,
            )
        except Exception as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            detail = safe_upstream_error_detail(exc, redact_identity=identity)
            logger.error("unexpected proxy error request_id=%s detail=%s", request_id, detail)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                status=500,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=500,
                    exc=exc,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                500,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
                redact_identity=identity,
            )
        finally:
            self._pre_response_deadline = None
            _restore_gateway_request(previous_admission)
            shutdown_controller.complete(admission)

    def _send_local_responses_no_content(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        write_proxy_event(
            "request_start",
            request_id=request_id,
            path=self.path,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_probe",
            content_length=0,
            **request_context,
        )
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("X-Codex-Proxy-Upstream", "local")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        write_proxy_event(
            "request_complete",
            request_id=request_id,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_probe",
            status=204,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **request_context,
        )

    def _handle_websocket_recording_probe(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        handshake_metadata = redacted_handshake_metadata(self.path, self.headers)
        selected_subprotocol = handshake_metadata.get("selected_subprotocol")
        key = _get_header(self.headers, "Sec-WebSocket-Key")
        if not key:
            self._send_json(400, {"error": "missing Sec-WebSocket-Key"})
            self.close_connection = True
            write_proxy_event(
                "websocket_probe_error",
                request_id=request_id,
                error="MissingSecWebSocketKey",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **handshake_metadata,
                **request_context,
            )
            return

        write_proxy_event(
            "websocket_probe_start",
            request_id=request_id,
            **handshake_metadata,
            **request_context,
        )
        self.send_response(101, "Switching Protocols")
        for header, value in websocket_upgrade_response_headers(key, selected_subprotocol if isinstance(selected_subprotocol, str) else None):
            self.send_header(header, value)
        self.end_headers()

        frames_recorded = 0
        close_code = None
        error_name = None
        stop_reason = "max_frames"
        max_frames = gateway_websocket_recorder_max_frames()
        recorder_idle_timeout = gateway_websocket_recorder_idle_timeout_seconds()
        connection = getattr(self, "connection", None)
        if connection is not None and hasattr(connection, "settimeout"):
            try:
                connection.settimeout(recorder_idle_timeout)
            except OSError:
                pass
        try:
            while frames_recorded < max_frames:
                try:
                    frame = read_frame(self.rfile, expect_masked=True, max_payload_bytes=1024 * 1024)
                except EOFError:
                    stop_reason = "eof"
                    break
                except TimeoutError:
                    stop_reason = "idle_timeout"
                    break
                frames_recorded += 1
                frame_metadata = _websocket_probe_frame_metadata(frame)
                write_proxy_event(
                    "websocket_probe_frame",
                    request_id=request_id,
                    frame_index=frames_recorded,
                    **frame_metadata,
                    **request_context,
                )
                if frame.opcode == 0x8:
                    close_code = frame_metadata.get("close_code")
                    stop_reason = "client_close"
                    break
        except WebSocketProtocolError as exc:
            error_name = type(exc).__name__
            write_proxy_event(
                "websocket_probe_error",
                request_id=request_id,
                error=error_name,
                detail=str(exc)[:160],
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **handshake_metadata,
                **request_context,
            )
        finally:
            try:
                write_frame(self.wfile, close_frame(1000, "recorded"), mask=False)
                self.wfile.flush()
            except OSError as exc:
                error_name = type(exc).__name__
                write_proxy_event(
                    "websocket_probe_error",
                    request_id=request_id,
                    error=error_name,
                    detail=safe_upstream_error_detail(exc),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **handshake_metadata,
                    **request_context,
                )
            self.close_connection = True

        write_proxy_event(
            "websocket_probe_complete",
            request_id=request_id,
            frames_recorded=frames_recorded,
            close_code=close_code,
            stop_reason=stop_reason,
            error=error_name,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **handshake_metadata,
            **request_context,
        )

    def _reject_local_responses_websocket_probe(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        write_proxy_event(
            "request_start",
            request_id=request_id,
            path=self.path,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_websocket_fast_reject",
            content_length=0,
            **request_context,
        )

        payload = {"detail": "WebSocket transport is not supported by this local Codex proxy; use POST /v1/responses."}
        body = _json_response_bytes(payload)
        self.send_response(405, "Method Not Allowed")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Codex-Proxy-Upstream", "local")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True
        write_proxy_event(
            "request_complete",
            request_id=request_id,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_websocket_fast_reject",
            status=405,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **request_context,
        )

    def _passthrough_official_control_request(self, method: str) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        proxy_request_context = _event_context_with_request_kind(request_context, RETRY_REQUEST_OFFICIAL_CONTROL)
        upstream = official_upstream()
        upstream_name = upstream["name"]
        relay_execution_plan = RelayExecutionPlan(
            selected_upstream_format=RouteProtocol.RESPONSES.value,
            request_kind=RETRY_REQUEST_OFFICIAL_CONTROL,
            streaming_policy=StreamingPolicy.GATEWAY_ADAPTED,
            usage_policy=UsagePolicy.SYNC_CAPTURE,
            response_mutation_policy=MutationPolicy.GATEWAY_COMPATIBILITY,
            sse_mutation_policy=MutationPolicy.GATEWAY_COMPATIBILITY,
            verify_cross_protocol_source=False,
            lifecycle_final_retry_enabled=False,
        )

        try:
            headers = upstream_headers(self.headers, upstream)
            write_proxy_event(
                "request_start",
                request_id=request_id,
                path=self.path,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                content_length=0,
                **proxy_request_context,
            )
            request = Request(_responses_url(upstream, self.path), headers=headers, method=method)
            adapter_event_context = {
                "request_id": request_id,
                "model": None,
                **proxy_request_context,
            }
            with _open_upstream_response(
                request,
                upstream_name=upstream_name,
                upstream_format="responses",
                timeout=upstream_timeout_seconds(),
                event_context=adapter_event_context,
                request_kind=RETRY_REQUEST_OFFICIAL_CONTROL,
            ) as response:
                status = self._relay_upstream_response(
                    response,
                    upstream_name,
                    request_id=request_id,
                    model=None,
                    relay_execution_plan=relay_execution_plan,
                )
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
        except HTTPError as exc:
            try:
                status = self._relay_upstream_response(
                    exc,
                    upstream_name,
                    request_id=request_id,
                    model=None,
                    relay_execution_plan=relay_execution_plan,
                )
            except OSError as relay_exc:
                self.close_connection = True
                write_proxy_event(
                    "client_write_failed",
                    request_id=request_id,
                    method=method,
                    model=None,
                    upstream=upstream_name,
                    route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                    status=getattr(exc, "code", 502),
                    error=type(relay_exc).__name__,
                    detail=safe_upstream_error_detail(relay_exc),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **proxy_request_context,
                )
                return
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                status=status,
                error="HTTPError",
                detail=safe_upstream_error_detail(exc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
        except (OSError, URLError) as exc:
            detail = safe_upstream_error_detail(exc)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                status=502,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            self._safe_send_json(502, {"error": type(exc).__name__, "detail": detail}, request_id)

    def _send_user_requested_shutdown_outcome(
        self,
        *,
        inbound_format: str,
        downstream_sse_started: bool,
    ) -> bool:
        payload = user_requested_shutdown_payload(inbound_format)
        try:
            if downstream_sse_started:
                if inbound_format == "chat_completions":
                    written = self._write_sse_data(payload)
                else:
                    written = self._write_sse_event("error", payload)
                self.close_connection = True
                return written
            self._send_json(503, payload)
        except OSError:
            self.close_connection = True
            return False
        self.close_connection = True
        return True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_response_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_and_close(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_response_bytes(payload)
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self._write_non_streaming_body_relay(body)

    def _relay_raw_upstream_response(self, response: Any, upstream_name: str) -> int:
        status = getattr(response, "status", None) or getattr(response, "code", 502)
        body = response.read()
        self.send_response(status)
        for key, value in _filtered_response_headers(
            response.headers,
            False,
            content_length=len(body),
        ):
            self.send_header(key, value)
        self.send_header("X-Codex-Proxy-Upstream", upstream_name)
        self.send_header("Connection", "close")
        self.end_headers()
        if not self._write_non_streaming_body_relay(body):
            return 499
        self.close_connection = True
        return status

    def _send_sse_headers(self, status: int, upstream_name: str) -> bool:
        seam = _handler_downstream_stream_commit(self)

        def _send() -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Codex-Proxy-Upstream", upstream_name)
            self.send_header("Connection", "close")
            self.end_headers()

        if seam is not None:
            return seam.commit_headers(status, _send)
        _send()
        return True

    def _write_sse_bytes(self, data: bytes, *, observe: bool = True) -> bool:
        """Commit arbitrary SSE bytes through the request-scoped seam if active.

        When no seam is active the bytes are written directly and any OSError is
        allowed to propagate so callers can recover the original exception.
        """
        seam = _handler_downstream_stream_commit(self)
        if seam is not None:
            return seam.commit_sse_bytes(data, observe=observe)
        self.wfile.write(data)
        self.wfile.flush()
        return True

    def _write_non_streaming_body_relay(self, body: bytes) -> bool:
        """Write a complete non-streaming response body directly to the downstream.

        This is the narrow allowlisted helper for non-streaming JSON and body-relay
        writes. It returns True on success and False when the downstream socket is
        closed, matching the seam contract so callers can surface a 499.
        """
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            self.close_connection = True
            return False
        return True

    def _write_sse_event(self, event: str, payload: Mapping[str, Any]) -> bool:
        data = (
            f"event: {event}\n".encode("utf-8")
            + b"data: "
            + json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            + b"\n\n"
        )
        return self._write_sse_bytes(data)

    def _write_sse_data(self, payload: Mapping[str, Any]) -> bool:
        return self._write_sse_bytes(_sse_json_line(payload, b"\n") + b"\n")

    def _write_sse_keepalive(self) -> bool:
        return self._write_sse_bytes(b": codexhub.keepalive\n\n", observe=False)

    def _write_sse_done(self) -> bool:
        seam = _handler_downstream_stream_commit(self)
        if seam is not None and seam.terminal_committed:
            # The protocol-specific terminal has already been committed; do not
            # write the legacy Chat [DONE] sentinel after Responses terminals.
            return True
        return self._write_sse_bytes(b"data: [DONE]\n\n")

    def _iter_upstream_sse_lines(
        self,
        response: Any,
        *,
        downstream_output_started: Callable[[], bool] | None = None,
        line_resets_idle_timeout: Callable[[bytes], bool] | None = None,
        on_line: Callable[[bytes], None] | None = None,
    ) -> Any:
        admission = _active_gateway_request()

        def raise_if_shutdown_requested() -> None:
            if admission is not None:
                admission.raise_if_cancelled()

        def observe_line(line: bytes) -> None:
            if not line or on_line is None:
                return
            try:
                on_line(line)
            except Exception:
                return

        keepalive_interval = sse_keepalive_seconds()
        transport_timeout_seconds = transport_sse_idle_timeout_seconds()
        model_event_timeout_seconds = model_event_sse_idle_timeout_seconds()
        transport_idle_guard_enabled = transport_timeout_seconds > 0
        model_event_idle_guard_enabled = model_event_timeout_seconds > 0 and line_resets_idle_timeout is not None

        lifecycle = _UpstreamSseReaderLifecycle(
            response,
            admission=admission,
        )
        request_scoped_seam = _handler_downstream_stream_commit(self)
        if request_scoped_seam is not None:
            request_scoped_seam.attach_upstream_response(lifecycle)
        lifecycle.start()
        try:
            stream_started_at = time.monotonic()
            last_transport_at = stream_started_at
            last_model_event_at = stream_started_at
            last_keepalive_at = stream_started_at

            def raise_idle_timeout(timeout_seconds: float, phase: str) -> None:
                lifecycle.close()
                raise UpstreamStreamIdleTimeoutError(timeout_seconds, phase=phase)

            while True:
                raise_if_shutdown_requested()
                now = time.monotonic()
                timeout_seconds: float | None = None
                if keepalive_interval > 0:
                    timeout_seconds = max(0.001, keepalive_interval - (now - last_keepalive_at))
                if transport_idle_guard_enabled:
                    remaining_idle = transport_timeout_seconds - (now - last_transport_at)
                    if remaining_idle <= 0:
                        raise_idle_timeout(transport_timeout_seconds, "transport")
                    timeout_seconds = (
                        remaining_idle
                        if timeout_seconds is None
                        else max(0.001, min(timeout_seconds, remaining_idle))
                    )
                if model_event_idle_guard_enabled:
                    remaining_idle = model_event_timeout_seconds - (now - last_model_event_at)
                    if remaining_idle <= 0:
                        raise_idle_timeout(model_event_timeout_seconds, "model_event")
                    timeout_seconds = (
                        remaining_idle
                        if timeout_seconds is None
                        else max(0.001, min(timeout_seconds, remaining_idle))
                    )
                if admission is not None:
                    timeout_seconds = (
                        0.1
                        if timeout_seconds is None
                        else max(0.001, min(timeout_seconds, 0.1))
                    )

                try:
                    if timeout_seconds is None:
                        kind, value = lifecycle.get()
                    else:
                        kind, value = lifecycle.get(timeout=timeout_seconds)
                except queue.Empty:
                    raise_if_shutdown_requested()
                    if lifecycle.closed:
                        return
                    now = time.monotonic()
                    if transport_idle_guard_enabled and (now - last_transport_at) >= transport_timeout_seconds:
                        raise_idle_timeout(transport_timeout_seconds, "transport")
                    if model_event_idle_guard_enabled and (now - last_model_event_at) >= model_event_timeout_seconds:
                        raise_idle_timeout(model_event_timeout_seconds, "model_event")
                    if keepalive_interval > 0:
                        if not self._write_sse_keepalive():
                            lifecycle.close()
                            raise DownstreamKeepaliveFailedError(
                                "downstream keepalive write failed"
                            )
                        last_keepalive_at = time.monotonic()
                    continue
                if kind == "error":
                    raise_if_shutdown_requested()
                    raise value
                if isinstance(value, bytes) and value:
                    now = time.monotonic()
                    last_transport_at = now
                    resets_model_event_timeout = (
                        line_resets_idle_timeout(value)
                        if line_resets_idle_timeout is not None
                        else False
                    )
                    if model_event_idle_guard_enabled and resets_model_event_timeout:
                        last_model_event_at = now
                    observe_line(value)
                yield value
                if not value:
                    return
        finally:
            lifecycle.close()
            lifecycle.join(timeout=_UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS)

    def _iter_upstream_sse_events(
        self,
        response: Any,
        *,
        event_resets_idle_timeout: Callable[[SseEvent], bool],
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> Any:
        assembler = SseEventAssembler()
        pending_events: list[SseEvent] = []
        assembler_finished = False
        deferred_size_error: SseFrameTooLargeError | None = None

        def assemble_chunk(chunk: bytes) -> bool:
            nonlocal deferred_size_error
            events: list[SseEvent] = []
            try:
                assembler.feed(chunk, on_event=events.append)
            except SseFrameTooLargeError as exc:
                deferred_size_error = exc
            pending_events.extend(events)
            return any(event_resets_idle_timeout(event) for event in events)

        try:
            for chunk in self._iter_upstream_sse_lines(
                response,
                line_resets_idle_timeout=assemble_chunk,
                on_line=on_chunk,
            ):
                if not chunk:
                    break
                ready_events = tuple(pending_events)
                pending_events.clear()
                yield from ready_events
                if deferred_size_error is not None:
                    raise deferred_size_error

            termination = assembler.finish()
            assembler_finished = True
            yield from termination.events
            if termination.disposition == "incomplete":
                raise UpstreamStreamIncompleteError(
                    "Upstream SSE stream ended with an incomplete pending frame"
                )
        finally:
            if not assembler_finished:
                try:
                    assembler.cancel()
                except SseAssemblerClosedError:
                    pass

    def _write_sse_error_event(
        self,
        upstream_name: str,
        exc: BaseException,
        *,
        redact_identity: str | None = None,
    ) -> None:
        self._write_sse_event(
            "error",
            _downstream_sse_error_payload_for_inbound_format(
                DownstreamErrorSpec(
                    inbound_format="responses",
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=redact_identity,
                )
            ),
        )

    def _write_downstream_sse_error(
        self,
        *,
        inbound_format: str,
        upstream_name: str,
        status: int = 502,
        exc: BaseException | None = None,
        error: str | None = None,
        detail: str | None = None,
        error_type: str = "upstream_error",
        preserve_explicit_error: bool = False,
        redact_identity: str | None = None,
    ) -> bool:
        error_spec = DownstreamErrorSpec(
            inbound_format=inbound_format,
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
            preserve_explicit_error=preserve_explicit_error,
            redact_identity=redact_identity,
        )
        if inbound_format == "chat_completions":
            data = (
                b"data: "
                + json.dumps(
                    _downstream_sse_error_payload_for_inbound_format(error_spec),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n\n"
            )
            if not self._write_sse_bytes(data):
                return False
            self.close_connection = True
            return True
        if not self._write_sse_event("error", _downstream_sse_error_payload_for_inbound_format(error_spec)):
            return False
        self.close_connection = True
        return True

    def _write_sse_protocol_error_event(
        self,
        upstream_name: str,
        status: int,
        detail: str,
        *,
        error: str = "UpstreamProtocolError",
        redact_identity: str | None = None,
    ) -> None:
        self._write_sse_event(
            "error",
            _downstream_sse_error_payload_for_inbound_format(
                DownstreamErrorSpec(
                    inbound_format="responses",
                    upstream_name=upstream_name,
                    status=status,
                    error=error,
                    detail=detail,
                    redact_identity=redact_identity,
                )
            ),
        )

    def _safe_send_downstream_json_error(
        self,
        status: int,
        *,
        inbound_format: str,
        upstream_name: str,
        request_id: str,
        exc: BaseException | None = None,
        error: str | None = None,
        detail: str | None = None,
        error_type: str = "upstream_error",
        redact_identity: str | None = None,
    ) -> None:
        error_spec = DownstreamErrorSpec(
            inbound_format=inbound_format,
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
            redact_identity=redact_identity,
        )
        self._safe_send_json(
            status,
            _downstream_json_error_payload(error_spec),
            request_id,
        )

    def _safe_send_json(self, status: int, payload: dict[str, Any], request_id: str) -> None:
        try:
            self._send_json(status, payload)
        except OSError as exc:
            self.close_connection = True
            write_proxy_event(
                "client_write_failed",
                request_id=request_id,
                status=status,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
            )

    def _relay_official_passthrough_sse_response(
        self,
        response: Any,
        upstream_name: str,
        *,
        request_id: str | None = None,
        model: str | None = None,
        upstream_format: str = "responses",
        inbound_format: str = "responses",
        usage_capture: dict[str, Any] | None = None,
        headers_already_sent: bool = False,
        mark_downstream_sse_started: Callable[[], None] | None = None,
        event_context: Mapping[str, Any] | None = None,
        defer_stream_errors: bool = False,
    ) -> int:
        status = getattr(response, "status", None) or getattr(response, "code", 502)
        headers_sent_downstream = bool(headers_already_sent)
        route_failure_event_fields = _route_failure_event_fields(event_context)
        _write_proxy_event = globals()["write_proxy_event"]

        def write_proxy_event(event: str, **fields: Any) -> None:
            enriched = dict(fields)
            enriched.update(route_failure_event_fields)
            _write_proxy_event(event, **enriched)

        admission = _active_gateway_request()
        request_scoped_seam = _handler_downstream_stream_commit(self)
        if request_scoped_seam is not None:
            request_scoped_seam.set_terminal_observer(_responses_terminal_observer)
            request_scoped_seam.set_synthetic_terminal_failure_callback(_responses_synthetic_terminal_failure)
            request_scoped_seam.set_usage_line_callback(_offer_official_passthrough_usage_line)
            seam = request_scoped_seam
        else:
            seam = _GatewayDownstreamStreamCommit(
                self,
                response,
                upstream_name,
                model=model,
                request_id=request_id,
                inbound_format=inbound_format,
                terminal_observer=_responses_terminal_observer,
                synthetic_terminal_failure_callback=_responses_synthetic_terminal_failure,
            )
        _capture_usage(usage_capture, None, missing_reason="async_official_passthrough")

        def _last_upstream_byte_age_ms(now: float, last_at: float | None) -> int | None:
            return None if last_at is None else int(max(0.0, now - last_at) * 1000)

        def _emit_stream_closed(
            *,
            status_code: int,
            error: str,
            detail: str,
            failure_phase: str,
            failure_side: str,
            failure_class: str,
            client_disconnected: bool,
            synthetic_terminal_event_sent: bool,
            synthetic_terminal_event_type: str | None,
            synthetic_terminal_write_error: str | None,
            synthetic_terminal_write_detail: str | None,
        ) -> None:
            close_phase = seam.close_phase
            counters = seam.counters()
            now = time.monotonic()
            write_proxy_event(
                "official_passthrough_stream_closed",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=status_code,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=error,
                detail=detail,
                failure_phase=failure_phase,
                failure_side=failure_side,
                failure_class=failure_class,
                client_disconnected=client_disconnected,
                synthetic_terminal_event_sent=synthetic_terminal_event_sent,
                synthetic_terminal_event_type=synthetic_terminal_event_type,
                synthetic_terminal_write_error=synthetic_terminal_write_error,
                synthetic_terminal_write_detail=synthetic_terminal_write_detail,
                lines_streamed=counters["lines_streamed"],
                bytes_streamed=counters["bytes_streamed"],
                last_upstream_byte_age_ms=_last_upstream_byte_age_ms(
                    now, counters["last_upstream_byte_at"]
                ),
                headers_sent_downstream=headers_sent_downstream,
                downstream_sse_started=headers_sent_downstream,
                close_phase=close_phase,
                **seam.stats(),
            )

        def _handle_downstream_header_failure() -> int:
            write_error = seam.last_write_error()
            exc = write_error if write_error is not None else OSError("downstream closed")
            _emit_stream_closed(
                status_code=499,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                failure_phase=seam.close_phase or "before_output",
                failure_side="downstream_write",
                failure_class="client_disconnected",
                client_disconnected=True,
                synthetic_terminal_event_sent=False,
                synthetic_terminal_event_type=None,
                synthetic_terminal_write_error=None,
                synthetic_terminal_write_detail=None,
            )
            return 499

        def send_downstream_headers_once() -> bool:
            nonlocal headers_sent_downstream
            if headers_sent_downstream:
                return True

            def _send() -> None:
                self.send_response(status)
                for key, value in _filtered_response_headers(response.headers, True):
                    self.send_header(key, value)
                self.send_header("X-Codex-Proxy-Upstream", upstream_name)
                self.send_header("Connection", "close")
                self.end_headers()

            if not seam.commit_headers(status, _send):
                return False
            headers_sent_downstream = True
            if mark_downstream_sse_started is not None:
                mark_downstream_sse_started()
            return True

        if not defer_stream_errors:
            if not send_downstream_headers_once():
                return _handle_downstream_header_failure()

        def _handle_cancellation() -> int:
            seam.cancel()
            _emit_stream_closed(
                status_code=503,
                error="GatewayUserRequestedShutdown",
                detail="request cancelled by gateway shutdown",
                failure_phase="upstream_read",
                failure_side="upstream_read",
                failure_class="gateway_shutdown",
                client_disconnected=False,
                synthetic_terminal_event_sent=False,
                synthetic_terminal_event_type=None,
                synthetic_terminal_write_error=None,
                synthetic_terminal_write_detail=None,
            )
            return 503

        def _observed_cancellation() -> int | None:
            """Return a status if cancellation was observed, honoring terminal commitment."""
            if admission is None or not admission.cancelled:
                return None
            if seam.terminal_committed:
                sse_fields = seam.stats()
                if usage_capture is not None:
                    usage_capture.update(sse_fields)
                return status
            return _handle_cancellation()

        lifecycle = _UpstreamSseReaderLifecycle(
            response,
            admission=admission,
        )
        seam.attach_upstream_response(lifecycle)
        try:
            while True:
                result = _observed_cancellation()
                if result is not None:
                    return result
                line = lifecycle.readline()
                result = _observed_cancellation()
                if result is not None:
                    return result
                if not line:
                    if defer_stream_errors and not headers_sent_downstream:
                        raise UpstreamStreamIncompleteError("Official stream ended before its first SSE byte")
                    break
                if not send_downstream_headers_once():
                    return _handle_downstream_header_failure()
                _observe_gateway_diagnostic("observe_sse_line", request_id, len(line))
                if not seam.commit_data(line):
                    if seam.terminal_committed:
                        # Terminal ledger is sealed; suppress the post-terminal
                        # upstream line without writing or mislabeling it as a
                        # downstream client disconnect.
                        return status
                    close_phase = seam.close_phase
                    _emit_stream_closed(
                        status_code=499,
                        error="OSError",
                        detail=f"downstream_client_closed ({close_phase})",
                        failure_phase="downstream_write",
                        failure_side="downstream_write",
                        failure_class="downstream_client_closed",
                        client_disconnected=True,
                        synthetic_terminal_event_sent=False,
                        synthetic_terminal_event_type=None,
                        synthetic_terminal_write_error=None,
                        synthetic_terminal_write_detail=None,
                    )
                    return 499
        except (IncompleteRead, TimeoutError, OSError, URLError, SseFrameTooLargeError) as exc:
            if seam.terminal_committed:
                sse_fields = seam.stats()
                if usage_capture is not None:
                    usage_capture.update(sse_fields)
                return status
            if admission is not None and admission.cancelled:
                return _handle_cancellation()
            if defer_stream_errors and not headers_sent_downstream:
                raise UpstreamStreamInterruptedError(exc) from exc
            if seam.downstream_closed:
                _emit_stream_closed(
                    status_code=499,
                    error=type(exc).__name__,
                    detail=safe_upstream_error_detail(exc),
                    failure_phase="stream_body",
                    failure_side="upstream_read",
                    failure_class="downstream_client_closed",
                    client_disconnected=True,
                    synthetic_terminal_event_sent=False,
                    synthetic_terminal_event_type=None,
                    synthetic_terminal_write_error=None,
                    synthetic_terminal_write_detail=None,
                )
                return 499
            (
                synthetic_terminal_event_sent,
                synthetic_terminal_write_error,
                synthetic_terminal_write_detail,
            ) = seam.commit_terminal_failure(exc, status=502)
            if seam.downstream_closed and seam.last_write_error() is not None:
                return _handle_downstream_header_failure()
            sse_fields = seam.stats()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
                usage_capture["synthetic_terminal_event_sent"] = synthetic_terminal_event_sent
                if synthetic_terminal_event_sent:
                    usage_capture["synthetic_terminal_event_type"] = "response.failed"
                if synthetic_terminal_write_error is not None:
                    usage_capture["synthetic_terminal_write_error"] = synthetic_terminal_write_error
            _emit_stream_closed(
                status_code=502,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                failure_phase="stream_body",
                failure_side="upstream_read",
                failure_class=getattr(exc, "classification", "upstream_stream_interrupted"),
                client_disconnected=False,
                synthetic_terminal_event_sent=synthetic_terminal_event_sent,
                synthetic_terminal_event_type="response.failed" if synthetic_terminal_event_sent else None,
                synthetic_terminal_write_error=synthetic_terminal_write_error,
                synthetic_terminal_write_detail=synthetic_terminal_write_detail,
            )
            return 502
        finally:
            lifecycle.close()
            lifecycle.join(timeout=_UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS)

        self.close_connection = True
        sse_fields = seam.stats()
        if usage_capture is not None:
            usage_capture.update(sse_fields)
        return status

    def _relay_transparent_upstream_response(
        self,
        response: Any,
        upstream_name: str,
        *,
        request_id: str | None = None,
        model: str | None = None,
        upstream_format: str = "responses",
        inbound_format: str = "responses",
        usage_capture: dict[str, Any] | None = None,
        headers_already_sent: bool = False,
        mark_downstream_sse_started: Callable[[], None] | None = None,
        event_context: Mapping[str, Any] | None = None,
        defer_stream_errors: bool = False,
    ) -> int:
        status = getattr(response, "status", None) or getattr(response, "code", 502)
        is_event_stream = _is_event_stream(response.headers)
        usage_context = _usage_observed_context(
            event_context,
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
        )
        relay_redact_identity = _retry_identity_from_context(event_context)
        route_failure_event_fields = _route_failure_event_fields(event_context)
        _write_proxy_event = globals()["write_proxy_event"]

        def write_proxy_event(event: str, **fields: Any) -> None:
            enriched = dict(fields)
            enriched.update(route_failure_event_fields)
            _write_proxy_event(event, **enriched)

        admission = _active_gateway_request()
        headers_sent = bool(headers_already_sent)
        chat_mode = inbound_format == "chat_completions"

        def _synthetic_terminal_failure_callback(
            handler: CodexProxyHandler,
            exc: BaseException,
            status: int,
            response_id: str | None,
            upstream_name: str,
            model: str | None,
        ) -> tuple[bool, str | None, str | None]:
            return _responses_synthetic_terminal_failure(
                handler,
                exc,
                status=status,
                response_id=response_id,
                upstream_name=upstream_name,
                model=model,
                redact_identity=relay_redact_identity,
            )

        request_scoped_seam = _handler_downstream_stream_commit(self)
        if request_scoped_seam is not None:
            request_scoped_seam.set_terminal_observer(
                _chat_terminal_observer if chat_mode else _responses_terminal_observer
            )
            request_scoped_seam.set_usage_line_callback(
                lambda context, line: _offer_usage_observed_sse_line(
                    context, line, upstream_format=upstream_format
                )
            )
            request_scoped_seam.set_synthetic_terminal_failure_callback(
                _synthetic_terminal_failure_callback if not chat_mode else None
            )
            seam = request_scoped_seam
        else:
            seam = _GatewayDownstreamStreamCommit(
                self,
                response,
                upstream_name,
                model=model,
                request_id=request_id,
                inbound_format=inbound_format,
                upstream_format=upstream_format,
                terminal_observer=(
                    _chat_terminal_observer if chat_mode else _responses_terminal_observer
                ),
                usage_line_callback=lambda context, line: _offer_usage_observed_sse_line(
                    context, line, upstream_format=upstream_format
                ),
                synthetic_terminal_failure_callback=(
                    _synthetic_terminal_failure_callback if not chat_mode else None
                ),
            )
        _capture_usage(usage_capture, None, missing_reason="async_usage_pending")

        def _handle_write_failure() -> int:
            """Emit downstream_stream_closed using the actual OSError when available.

            If commit_data returned False because the terminal was already
            committed, no client disconnect occurred; just return success.
            """
            close_phase = seam.close_phase
            write_error = seam.last_write_error()
            if write_error is None and seam.terminal_committed:
                # Stopped only because a terminal event was already committed.
                return status
            exc = write_error if write_error is not None else OSError("downstream closed")
            event_fields = _bounded_failure_event_context(event_context)
            for key in (
                "request_id",
                "model",
                "upstream",
                "status",
                "upstream_format",
                "inbound_format",
                "error",
                "detail",
            ):
                event_fields.pop(key, None)
            write_proxy_event(
                "downstream_stream_closed",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=499,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                close_phase=close_phase,
                failure_phase="downstream_write",
                failure_side="downstream_write",
                failure_class="downstream_client_closed",
                client_disconnected=True,
                terminal=seam.terminal_committed,
                terminal_seen=seam._sse_stats.terminal_event_seen,
                downstream_output_started=seam._downstream_output_started,
                retry_forbidden=True,
                retry_safety_class=(
                    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
                    if seam._downstream_content_exposed or seam._downstream_output_started
                    else RETRY_SAFETY_SUPPRESSED_POST_WRITE
                ),
                **event_fields,
            )
            return 499

        def send_downstream_headers_once(
            content_length: int | None = None,
            content_encoding: str | None | object = _UNSET_CONTENT_ENCODING,
        ) -> bool:
            nonlocal headers_sent
            if headers_sent:
                return True

            def _send() -> None:
                self.send_response(status)
                for key, value in _filtered_response_headers(
                    response.headers,
                    is_event_stream,
                    content_length=content_length,
                    content_encoding=content_encoding,
                ):
                    self.send_header(key, value)
                self.send_header("X-Codex-Proxy-Upstream", upstream_name)
                self.send_header("Connection", "close")
                self.end_headers()

            if not seam.commit_headers(status, _send):
                return False
            headers_sent = True
            if is_event_stream and mark_downstream_sse_started is not None:
                mark_downstream_sse_started()
            return True

        if is_event_stream and not (defer_stream_errors and not headers_already_sent):
            if not send_downstream_headers_once():
                return _handle_write_failure()

        def _last_upstream_byte_age_ms(now: float, last_at: float | None) -> int | None:
            return None if last_at is None else int(max(0.0, now - last_at) * 1000)

        def _emit_stream_closed(
            *,
            status_code: int,
            error: str,
            detail: str,
            failure_phase: str,
            failure_side: str,
            failure_class: str,
            client_disconnected: bool,
            synthetic_terminal_event_sent: bool,
            synthetic_terminal_event_type: str | None,
            synthetic_terminal_write_error: str | None,
            synthetic_terminal_write_detail: str | None,
        ) -> None:
            close_phase = seam.close_phase
            counters = seam.counters()
            now = time.monotonic()
            write_proxy_event(
                "transparent_stream_closed",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=status_code,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=error,
                detail=_redact_identity_in_text(detail, relay_redact_identity),
                failure_phase=failure_phase,
                failure_side=failure_side,
                failure_class=failure_class,
                client_disconnected=client_disconnected,
                synthetic_terminal_event_sent=synthetic_terminal_event_sent,
                synthetic_terminal_event_type=synthetic_terminal_event_type,
                synthetic_terminal_write_error=synthetic_terminal_write_error,
                synthetic_terminal_write_detail=synthetic_terminal_write_detail,
                lines_streamed=counters["lines_streamed"],
                bytes_streamed=counters["bytes_streamed"],
                last_upstream_byte_age_ms=_last_upstream_byte_age_ms(
                    now, counters["last_upstream_byte_at"]
                ),
                headers_sent_downstream=headers_sent,
                downstream_sse_started=headers_sent,
                close_phase=close_phase,
                **route_failure_event_fields,
                **seam.stats(),
            )

        def _handle_cancellation() -> int:
            seam.cancel()
            _emit_stream_closed(
                status_code=503,
                error="GatewayUserRequestedShutdown",
                detail="request cancelled by gateway shutdown",
                failure_phase="upstream_read",
                failure_side="upstream_read",
                failure_class="gateway_shutdown",
                client_disconnected=False,
                synthetic_terminal_event_sent=False,
                synthetic_terminal_event_type=None,
                synthetic_terminal_write_error=None,
                synthetic_terminal_write_detail=None,
            )
            return 503

        def _observed_cancellation() -> int | None:
            """Return a status if cancellation was observed, honoring terminal commitment."""
            if admission is None or not admission.cancelled:
                return None
            if seam.terminal_committed:
                sse_fields = seam.stats()
                if usage_capture is not None:
                    usage_capture.update(sse_fields)
                return status
            return _handle_cancellation()

        def _send_terminal_json_error(
            status_code: int,
            detail: str,
            error_type: str = "upstream_error",
            *,
            telemetry_event: str | None = None,
            telemetry_error: str | None = None,
        ) -> int:
            sanitized_detail = _redact_identity_in_text(detail, relay_redact_identity)
            if telemetry_event is not None:
                write_proxy_event(
                    telemetry_event,
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=status_code,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    error=telemetry_error or error_type,
                    detail=sanitized_detail,
                    **route_failure_event_fields,
                )
            if inbound_format == "chat_completions":
                terminal_payload = _chat_completion_error_payload(
                    upstream_name=upstream_name,
                    status=status_code,
                    detail=sanitized_detail,
                    error_type=error_type,
                    redact_identity=relay_redact_identity,
                )
            else:
                terminal_payload = _downstream_stream_error_payload(
                    upstream_name=upstream_name,
                    status=status_code,
                    detail=sanitized_detail,
                    error_type=error_type,
                    redact_identity=relay_redact_identity,
                )
            self._send_json(status_code, terminal_payload)
            self.close_connection = True
            return status_code

        if not is_event_stream:
            body = b""
            try:
                while True:
                    result = _observed_cancellation()
                    if result is not None:
                        return result
                    chunk = response.read(65536)
                    result = _observed_cancellation()
                    if result is not None:
                        return result
                    if not chunk:
                        break
                    body += chunk
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if seam.terminal_committed:
                    return status
                if admission is not None and admission.cancelled:
                    return _handle_cancellation()
                return _send_terminal_json_error(
                    502,
                    safe_upstream_error_detail(exc, redact_identity=relay_redact_identity),
                    telemetry_event="transparent_body_read_failed",
                    telemetry_error=type(exc).__name__,
                )

            drop_content_encoding = False
            if relay_redact_identity is not None and status >= 400:
                content_encoding_value = _get_header(response.headers, "Content-Encoding")
                if content_encoding_value:
                    decoded_body, did_decode, decode_error = decoded_request_body(
                        body, content_encoding_value
                    )
                    if did_decode:
                        body = decoded_body
                        drop_content_encoding = True
                    else:
                        detail = (
                            f"upstream {status} response body uses unsupported or malformed "
                            f"Content-Encoding ({content_encoding_value}); cannot safely relay"
                        )
                        if decode_error:
                            detail = f"{detail}: {decode_error}"
                        return _send_terminal_json_error(
                            502,
                            detail,
                            error_type="upstream_protocol_error",
                            telemetry_event="transparent_body_decode_failed",
                            telemetry_error="ContentEncodingDecodeError",
                        )
                body = body.replace(
                    relay_redact_identity.encode("utf-8"),
                    b"[retry_identity_redacted]",
                )
            content_encoding = None if drop_content_encoding else _UNSET_CONTENT_ENCODING
            if not send_downstream_headers_once(
                content_length=len(body),
                content_encoding=content_encoding,
            ):
                return _handle_write_failure()
            if not self._write_non_streaming_body_relay(body):
                return _handle_write_failure()
            _offer_usage_observed_body(usage_context, body)
            self.close_connection = True
            return status

        pending_lines: list[bytes] = []

        def transparent_error_event(payload: Mapping[str, Any]) -> UpstreamStreamErrorEvent | None:
            if upstream_format == "responses" and _responses_stream_error_type(payload) is not None:
                return UpstreamStreamErrorEvent(payload)
            if upstream_format == "chat_completions" and _chat_stream_error_detail(payload) is not None:
                return UpstreamStreamErrorEvent(payload)
            return None

        def _commit_pending_lines() -> bool:
            """Commit buffered pending lines through the seam. Returns True on success."""
            for pending_line in pending_lines:
                if not seam.commit_data(pending_line):
                    return False
            pending_lines.clear()
            return True

        def _handle_stream_failure(exc: BaseException) -> int:
            result = _observed_cancellation()
            if result is not None:
                return result
            if seam.terminal_committed:
                sse_fields = seam.stats()
                if usage_capture is not None:
                    usage_capture.update(sse_fields)
                return status
            if defer_stream_errors and not headers_sent:
                raise UpstreamStreamInterruptedError(exc) from exc
            stream_failure_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
            if seam.downstream_closed:
                _emit_stream_closed(
                    status_code=499,
                    error=type(exc).__name__,
                    detail=stream_failure_detail,
                    failure_phase="stream_body",
                    failure_side="upstream_read",
                    failure_class="downstream_client_closed",
                    client_disconnected=True,
                    synthetic_terminal_event_sent=False,
                    synthetic_terminal_event_type=None,
                    synthetic_terminal_write_error=None,
                    synthetic_terminal_write_detail=None,
                )
                return 499
            (
                synthetic_terminal_event_sent,
                synthetic_terminal_write_error,
                synthetic_terminal_write_detail,
            ) = seam.commit_terminal_failure(exc, status=502)
            if seam.downstream_closed and seam.last_write_error() is not None:
                return _handle_write_failure()
            sse_fields = seam.stats()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
                usage_capture["synthetic_terminal_event_sent"] = synthetic_terminal_event_sent
                if synthetic_terminal_event_sent:
                    usage_capture["synthetic_terminal_event_type"] = (
                        "response.failed" if inbound_format == "responses" else "chat.error"
                    )
                if synthetic_terminal_write_error is not None:
                    usage_capture["synthetic_terminal_write_error"] = synthetic_terminal_write_error
            synthetic_terminal_event_type = None
            if synthetic_terminal_event_sent:
                synthetic_terminal_event_type = (
                    "response.failed" if inbound_format == "responses" else "chat.error"
                )
            self.close_connection = True
            _emit_stream_closed(
                status_code=502,
                error=type(exc).__name__,
                detail=stream_failure_detail,
                failure_phase="stream_body",
                failure_side="upstream_read",
                failure_class=getattr(exc, "classification", "upstream_stream_interrupted"),
                client_disconnected=False,
                synthetic_terminal_event_sent=synthetic_terminal_event_sent,
                synthetic_terminal_event_type=synthetic_terminal_event_type,
                synthetic_terminal_write_error=synthetic_terminal_write_error,
                synthetic_terminal_write_detail=synthetic_terminal_write_detail,
            )
            return 502

        lifecycle = _UpstreamSseReaderLifecycle(
            response,
            admission=admission,
        )
        seam.attach_upstream_response(lifecycle)
        try:
            while True:
                result = _observed_cancellation()
                if result is not None:
                    return result
                try:
                    line = lifecycle.readline()
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    return _handle_stream_failure(exc)
                result = _observed_cancellation()
                if result is not None:
                    return result
                if not line:
                    break
                _observe_gateway_diagnostic("observe_sse_line", request_id, len(line))
                if defer_stream_errors and not headers_sent:
                    pending_lines.append(line)
                    payload_bytes = _sse_payload_bytes(line)
                    if payload_bytes is None:
                        continue
                    release_pending = True
                    if payload_bytes != b"[DONE]":
                        try:
                            payload = json.loads(payload_bytes.decode("utf-8-sig"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            payload = None
                        if isinstance(payload, Mapping):
                            stream_error = transparent_error_event(payload)
                            if stream_error is not None:
                                raise stream_error
                            if upstream_format == "responses":
                                event_type = payload.get("type")
                                release_pending = (
                                    event_type == "response.completed"
                                    or _responses_event_starts_downstream_output(payload)
                                )
                    if not release_pending:
                        continue
                    if not send_downstream_headers_once():
                        return _handle_write_failure()
                    if not _commit_pending_lines():
                        return _handle_write_failure()
                    continue
                if not send_downstream_headers_once():
                    return _handle_write_failure()
                if not seam.commit_data(line):
                    return _handle_write_failure()
            if pending_lines and not headers_sent:
                if not send_downstream_headers_once():
                    return _handle_write_failure()
                if not _commit_pending_lines():
                    return _handle_write_failure()
            self.close_connection = True
            sse_fields = seam.stats()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
            return status
        except SseFrameTooLargeError as exc:
            return _handle_stream_failure(exc)
        except UpstreamStreamErrorEvent:
            # Stream error events are intentionally raised without sending headers
            # so the caller can retry. The seam owns any bytes already committed.
            raise
        finally:
            lifecycle.close()
            lifecycle.join(timeout=_UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS)

    def _relay_upstream_response(
        self,
        response: Any,
        upstream_name: str,
        relay_execution_plan: RelayExecutionPlan,
        request_id: str | None = None,
        model: str | None = None,
        inbound_format: str = "responses",
        caller_stream: bool = True,
        event_context: Mapping[str, Any] | None = None,
        usage_capture: dict[str, Any] | None = None,
        headers_already_sent: bool = False,
        defer_stream_errors: bool = False,
        mark_downstream_sse_started: Callable[[], None] | None = None,
        response_lifecycle_state: dict[str, str] | None = None,
    ) -> int:
        upstream_format = relay_execution_plan.selected_upstream_format
        request_kind = relay_execution_plan.request_kind
        streaming_policy = relay_execution_plan.streaming_policy
        usage_policy = relay_execution_plan.usage_policy
        response_mutation_policy = (
            relay_execution_plan.response_mutation_policy
        )
        sse_mutation_policy = relay_execution_plan.sse_mutation_policy
        verify_cross_protocol_source = (
            relay_execution_plan.verify_cross_protocol_source
        )
        lifecycle_final_retry_enabled = (
            relay_execution_plan.lifecycle_final_retry_enabled
        )
        status = getattr(response, "status", None) or getattr(response, "code", 502)
        is_event_stream = _is_event_stream(response.headers)
        # When the caller spoke Chat Completions, the response must be converted
        # back to Chat Completions format regardless of the upstream wire format.
        want_chat_output = inbound_format == "chat_completions"
        request_scoped_seam = _handler_downstream_stream_commit(self)
        seam: _GatewayDownstreamStreamCommit | None = request_scoped_seam
        if request_scoped_seam is not None:
            request_scoped_seam.set_terminal_observer(
                _chat_terminal_observer if want_chat_output else _responses_terminal_observer
            )
            request_scoped_seam.set_usage_line_callback(
                lambda context, line: _offer_usage_observed_sse_line(
                    context, line, upstream_format=upstream_format
                )
            )
        else:
            seam = _GatewayDownstreamStreamCommit(
                self,
                response,
                upstream_name,
                model=model,
                request_id=request_id,
                inbound_format=inbound_format,
                upstream_format=upstream_format,
                terminal_observer=(
                    _chat_terminal_observer if want_chat_output else _responses_terminal_observer
                ),
                usage_line_callback=lambda context, line: _offer_usage_observed_sse_line(
                    context, line, upstream_format=upstream_format
                ),
            )
            self._downstream_stream_commit = seam
        compatibility_event_context = dict(event_context or {})
        compatibility_event_context["_apply_patch_adapter_enabled"] = not want_chat_output
        # When the caller asked for a non-streaming response but the upstream
        # returns SSE (e.g. chatgpt.com forces stream=true), buffer the entire
        # SSE into a single JSON response body.
        buffer_sse_to_json = is_event_stream and not caller_stream
        buffered_json_response = False
        buffered_chat_sse_to_responses = False
        verified_source_format = (
            upstream_format
            if (
                verify_cross_protocol_source
                and upstream_format != inbound_format
            )
            else None
        )
        usage_context = _usage_observed_context(
            event_context,
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
        )
        relay_redact_identity = _retry_identity_from_context(event_context)
        route_failure_event_fields = _route_failure_event_fields(event_context)
        _write_proxy_event = globals()["write_proxy_event"]

        def write_proxy_event(event: str, **fields: Any) -> None:
            enriched = dict(fields)
            enriched.update(route_failure_event_fields)
            _write_proxy_event(event, **enriched)

        def observe_diagnostic_sse_line(line: bytes) -> None:
            _observe_gateway_diagnostic("observe_sse_line", request_id, len(line))

        def remember_response_id(payload: Mapping[str, Any]) -> None:
            if response_lifecycle_state is None:
                return
            response_payload = payload.get("response")
            if not isinstance(response_payload, Mapping):
                return
            response_id = response_payload.get("id")
            if isinstance(response_id, str) and response_id:
                response_lifecycle_state["response_id"] = response_id

        if (
            streaming_policy == StreamingPolicy.TRANSPARENT
            and upstream_format == inbound_format
            and not (is_event_stream and not caller_stream and upstream_format == "responses")
        ):
            return self._relay_transparent_upstream_response(
                response,
                upstream_name,
                request_id=request_id,
                model=model,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                usage_capture=usage_capture,
                headers_already_sent=headers_already_sent,
                mark_downstream_sse_started=mark_downstream_sse_started,
                event_context=event_context,
                defer_stream_errors=defer_stream_errors,
            )
        if (
            streaming_policy == StreamingPolicy.OFFICIAL_PASSTHROUGH
            and is_event_stream
            and inbound_format == "responses"
            and upstream_format == "responses"
            and not want_chat_output
        ):
            return self._relay_official_passthrough_sse_response(
                response,
                upstream_name,
                request_id=request_id,
                model=model,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                usage_capture=usage_capture,
                headers_already_sent=headers_already_sent,
                mark_downstream_sse_started=mark_downstream_sse_started,
                event_context=event_context,
                defer_stream_errors=defer_stream_errors,
            )
        defer_stream_headers = (
            is_event_stream
            and caller_stream
            and lifecycle_final_retry_enabled
        )

        def finish_downstream_stream_closed(exc: OSError) -> int:
            self.close_connection = True
            event_fields = _bounded_failure_event_context(event_context)
            for key in ("request_id", "model", "upstream", "status", "error", "detail"):
                event_fields.pop(key, None)
            write_proxy_event(
                "downstream_stream_closed",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=499,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                failure_phase="downstream_write",
                failure_side="downstream_write",
                failure_class="downstream_client_closed",
                client_disconnected=True,
                terminal=seam.terminal_committed,
                terminal_seen=seam._sse_stats.terminal_event_seen,
                downstream_output_started=seam._downstream_output_started,
                retry_forbidden=True,
                retry_safety_class=(
                    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
                    if seam._downstream_content_exposed or seam._downstream_output_started
                    else RETRY_SAFETY_SUPPRESSED_POST_WRITE
                ),
                **event_fields,
            )
            _capture_usage(
                usage_capture,
                None,
                missing_reason="async_usage_pending"
                if usage_policy == UsagePolicy.ASYNC_TAP
                else "client_disconnected",
            )
            return 499

        def finish_converted_sse_semantic_error(
            exc: UpstreamSseSemanticError | SseFrameTooLargeError,
        ) -> int:
            if seam.terminal_committed:
                self.close_connection = True
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="async_usage_pending",
                )
                return status
            error_code = getattr(exc, "classification", "upstream_protocol_error")
            self.close_connection = True
            write_proxy_event(
                "upstream_stream_protocol_error",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=502,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=type(exc).__name__,
                detail=str(exc),
            )
            if not send_downstream_response_headers_once():
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            if not self._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=502,
                error=error_code,
                detail=str(exc),
                redact_identity=relay_redact_identity,
            ):
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            _capture_usage(usage_capture, None, missing_reason="stream_protocol_error")
            return 502

        def buffered_protocol_error_body(
            exc: UpstreamSseSemanticError | SseFrameTooLargeError,
        ) -> bytes:
            write_proxy_event(
                "upstream_stream_protocol_error",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=502,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=type(exc).__name__,
                detail=str(exc),
            )
            return json.dumps(
                _json_error_payload_for_inbound_format(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_protocol_error",
                    detail=str(exc),
                    error_type="upstream_protocol_error",
                    redact_identity=relay_redact_identity,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")

        headers_sent = headers_already_sent
        if not is_event_stream or buffer_sse_to_json:
            converted_stream_failure = False
            if buffer_sse_to_json:
                # Buffer the full SSE stream into a list of events.
                events: list[Mapping[str, Any]] = []
                chat_chunks: list[Mapping[str, Any] | str] = []
                incomplete_frame = False

                try:
                    for frame in self._iter_upstream_sse_events(
                        response,
                        event_resets_idle_timeout=(
                            _chat_sse_event_resets_idle_timeout
                            if upstream_format == "chat_completions"
                            else _responses_sse_event_resets_idle_timeout
                        ),
                        on_chunk=observe_diagnostic_sse_line,
                    ):
                        payload = _converted_sse_payload(
                            frame,
                            verified_source_format=verified_source_format,
                        )
                        if payload is None:
                            continue
                        if upstream_format == "chat_completions":
                            chat_chunks.append(payload)
                        elif payload != "[DONE]":
                            events.append(payload)
                except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                    status = 502
                    converted_stream_failure = True
                    body = buffered_protocol_error_body(exc)
                except UpstreamStreamIncompleteError:
                    incomplete_frame = True
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    raise
                # Reconstruct a Responses-format body from the events.
                if not converted_stream_failure:
                    try:
                        if incomplete_frame:
                            raise UpstreamStreamIncompleteError(
                                "Upstream SSE stream ended with an incomplete pending frame"
                            )
                        if (
                            upstream_format == "chat_completions"
                            and not want_chat_output
                        ):
                            response_events = _chat_stream_chunks_to_response_events(
                                chat_chunks
                            )
                            body = _events_to_responses_body(
                                response_events,
                                require_completed=True,
                            )
                            buffered_chat_sse_to_responses = True
                        else:
                            body = _events_to_responses_body(
                                events,
                                require_completed=True,
                            )
                    except UpstreamStreamIncompleteError:
                        if defer_stream_errors:
                            raise
                        status = 502
                        converted_stream_failure = True
                        body = _incomplete_stream_json_error_body(upstream_name)
                        write_proxy_event(
                            "upstream_stream_incomplete",
                            request_id=request_id,
                            model=model,
                            upstream=upstream_name,
                            status=status,
                            upstream_format=upstream_format,
                            inbound_format=inbound_format,
                        )
                    except UpstreamProtocolTranslationError:
                        if verified_source_format is None:
                            raise
                        status = 502
                        converted_stream_failure = True
                        body = buffered_protocol_error_body(
                            _verified_converted_sse_semantic_error(
                                verified_source_format
                            )
                        )
                is_event_stream = False
                buffered_json_response = True
            else:
                body = b""
                try:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        body += chunk
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    raise
            upstream_body_for_usage = body
            try:
                if converted_stream_failure:
                    pass
                elif want_chat_output:
                    if upstream_format == "chat_completions":
                        body = _response_body_to_chat_completion_body(
                            compatible_response_body(
                                _chat_completion_to_response_body(body),
                                upstream_name,
                                event_context=compatibility_event_context,
                            )
                        )
                    else:
                        # Upstream returned Responses format; convert to Chat Completions.
                        if (
                            response_mutation_policy
                            == MutationPolicy.TRANSPARENT
                        ):
                            body = _response_body_to_chat_completion_body(body)
                        else:
                            body = _response_body_to_chat_completion_body(
                                compatible_response_body(
                                    body,
                                    upstream_name,
                                    event_context=compatibility_event_context,
                                )
                            )
                elif upstream_format == "chat_completions":
                    if buffered_chat_sse_to_responses:
                        converted_body = body
                    else:
                        converted_body = _chat_completion_to_response_body(
                            body,
                            repair=(
                                response_mutation_policy
                                != MutationPolicy.TRANSPARENT
                            ),
                        )
                    if (
                        response_mutation_policy
                        == MutationPolicy.TRANSPARENT
                    ):
                        body = converted_body
                    else:
                        body = compatible_response_body(
                            converted_body,
                            upstream_name,
                            event_context=compatibility_event_context,
                        )
                else:
                    body = compatible_response_body(
                        body,
                        upstream_name,
                        event_context=compatibility_event_context,
                    )
            except UpstreamProtocolTranslationError:
                if not buffer_sse_to_json or verified_source_format is None:
                    raise
                status = 502
                converted_stream_failure = True
                body = buffered_protocol_error_body(
                    _verified_converted_sse_semantic_error(
                        verified_source_format
                    )
                )
            if status >= 400:
                body = _with_codexhub_http_error(
                    body,
                    upstream_name=upstream_name,
                    status=status,
                    exc=response if isinstance(response, BaseException) else None,
                )
            if usage_policy == UsagePolicy.ASYNC_TAP:
                _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
                _offer_usage_observed_body(usage_context, upstream_body_for_usage)
            else:
                _capture_usage(usage_capture, _usage_from_json_body(body))
                if status < 400:
                    lifecycle_issue = _response_body_lifecycle_final_issue(body, event_context, request_kind)
                    if lifecycle_issue is not None:
                        _write_adapter_event(
                            event_context,
                            _lifecycle_final_issue_event_name(lifecycle_issue),
                            upstream=upstream_name,
                            inbound_format=inbound_format,
                            want_chat_output=want_chat_output,
                            body_format="chat_completions" if want_chat_output else "responses",
                        )
                        _capture_usage(
                            usage_capture,
                            None,
                            missing_reason=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                        )
                        if not headers_already_sent:
                            _raise_lifecycle_final_issue(upstream_name, lifecycle_issue)
                        status = 502
                        body = json.dumps(
                            _json_error_payload_for_inbound_format(
                                inbound_format=inbound_format,
                                upstream_name=upstream_name,
                                status=status,
                                error=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                                detail=(
                                    "Upstream returned an empty final response after completed subagent lifecycle."
                                    if lifecycle_issue == "empty"
                                    else "Upstream returned a final response with extra text outside the requested report format."
                                ),
                                error_type=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                                redact_identity=relay_redact_identity,
                            ),
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
            if (
                status < 400
                and request_kind == RETRY_REQUEST_COMPACT
                and _compact_response_body_is_empty(body, inbound_format)
            ):
                if not headers_already_sent:
                    _capture_usage(usage_capture, None, missing_reason="compact_empty_response")
                    raise CompactEmptyResponseError(upstream_name)
                status = 502
                body = json.dumps(
                    _json_error_payload_for_inbound_format(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=status,
                        error="compact_empty_response",
                        detail="Upstream returned an empty compact summary.",
                        error_type="compact_empty_response",
                        redact_identity=relay_redact_identity,
                    ),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                event_fields = _public_event_context(event_context)
                event_fields.pop("request_id", None)
                event_fields.pop("model", None)
                event_fields.pop("upstream", None)
                event_fields.pop("status", None)
                write_proxy_event(
                    "compact_empty_response",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=status,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    **event_fields,
                )
                _capture_usage(usage_capture, None, missing_reason="compact_empty_response")
            else:
                empty_non_compact = (
                    _chat_completion_body_is_empty(body)
                    if inbound_format == "chat_completions"
                    else _responses_body_is_empty(body)
                )
                if status < 400 and request_kind != RETRY_REQUEST_COMPACT and empty_non_compact:
                    event_fields = _public_event_context(event_context)
                    event_fields.pop("request_id", None)
                    event_fields.pop("model", None)
                    event_fields.pop("upstream", None)
                    write_proxy_event(
                        "empty_assistant_response",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=status,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        **event_fields,
                    )
            downstream_expects_sse = caller_stream and (
                headers_sent or mark_downstream_sse_started is not None
            )
            if downstream_expects_sse and not want_chat_output and status < 400:
                try:
                    response_events = _response_body_to_response_sse_events(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response_events = []
                if response_events:
                    if not headers_sent:
                        if not self._send_sse_headers(status, upstream_name):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                        headers_sent = True
                        if mark_downstream_sse_started is not None:
                            mark_downstream_sse_started()
                    for event in response_events:
                        if (
                            sse_mutation_policy
                            != MutationPolicy.TRANSPARENT
                        ):
                            event, _ = _normalize_third_party_tool_call(event, compatibility_event_context)
                            event, _ = _suppress_bounded_tool_search_calls(
                                event,
                                compatibility_event_context,
                            )
                            if event is None:
                                continue
                            event, _ = _downgrade_invalid_third_party_tool_calls(event)
                            event, _ = _guard_duplicate_multi_agent_spawn_calls(event, compatibility_event_context)
                        event_type = event.get("type")
                        if isinstance(event_type, str) and event_type:
                            if not self._write_sse_event(event_type, event):
                                return finish_downstream_stream_closed(
                                    seam.last_write_error() or OSError("downstream closed")
                                )
                    sse_seam = _handler_downstream_stream_commit(self)
                    if (
                        sse_seam is not None
                        and sse_seam.downstream_closed
                        and sse_seam.last_write_error() is not None
                    ):
                        return finish_downstream_stream_closed(
                            sse_seam.last_write_error() or OSError("downstream closed")
                        )
                    self.close_connection = True
                    _capture_usage(
                        usage_capture,
                        None,
                        missing_reason="async_usage_pending"
                        if usage_policy == UsagePolicy.ASYNC_TAP
                        else "upstream_missing_usage",
                    )
                    return status
            if headers_sent:
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=status,
                    error="UpstreamProtocolError",
                    detail=f"upstream returned non-SSE response after downstream SSE retry status: HTTP {status}",
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                self.close_connection = True
                _capture_usage(usage_capture, None, missing_reason="stream_protocol_error")
                return status

        def send_downstream_response_headers_once() -> bool:
            nonlocal headers_sent
            if headers_sent:
                return True
            content_length = None if is_event_stream else len(body)
            content_type = "application/json" if buffered_json_response else None

            def _send() -> None:
                self.send_response(status)
                for key, value in _filtered_response_headers(
                    response.headers,
                    is_event_stream,
                    content_length,
                    content_type=content_type,
                ):
                    self.send_header(key, value)
                self.send_header("X-Codex-Proxy-Upstream", upstream_name)
                self.send_header("Connection", "close")
                self.end_headers()

            if not seam.commit_headers(status, _send):
                return False
            headers_sent = True
            if mark_downstream_sse_started is not None:
                mark_downstream_sse_started()
            return True

        seam.set_ensure_headers_committed_callback(
            send_downstream_response_headers_once if defer_stream_headers else None
        )
        if not defer_stream_headers:
            if not send_downstream_response_headers_once():
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )

        if is_event_stream:
            if (
                streaming_policy == StreamingPolicy.TRANSPARENT_CONVERTED
                and want_chat_output
                and upstream_format != "chat_completions"
            ):
                line_ending = b"\n"
                converter = _ResponsesToChatStreamConverter()
                incomplete_frame = False
                try:
                    for frame in self._iter_upstream_sse_events(
                        response,
                        event_resets_idle_timeout=_responses_sse_event_resets_idle_timeout,
                        on_chunk=observe_diagnostic_sse_line,
                    ):
                        line_ending = _sse_line_ending(frame.raw)
                        payload = _converted_sse_payload(
                            frame,
                            verified_source_format=verified_source_format,
                        )
                        if payload is None or payload == "[DONE]":
                            continue
                        event = payload
                        _offer_usage_observed_sse_line(
                            usage_context,
                            frame.raw,
                            upstream_format=upstream_format,
                        )
                        error_type = _responses_stream_error_type(event)
                        if error_type is not None:
                            detail = _redact_identity_in_text(
                                _responses_stream_error_detail(event),
                                relay_redact_identity,
                            )
                            write_proxy_event(
                                "upstream_stream_error_event",
                                request_id=request_id,
                                model=model,
                                upstream=upstream_name,
                                status=502,
                                upstream_format=upstream_format,
                                inbound_format=inbound_format,
                                error=error_type,
                                detail=detail,
                            )
                            if not self._write_downstream_sse_error(
                                inbound_format=inbound_format,
                                upstream_name=upstream_name,
                                status=502,
                                error=error_type,
                                detail=detail,
                                redact_identity=relay_redact_identity,
                            ):
                                return finish_downstream_stream_closed(
                                    seam.last_write_error() or OSError("downstream closed")
                                )
                            _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                            return 502
                        for chunk in converter.chunks_for_event(event):
                            if not self._write_sse_data(chunk):
                                return finish_downstream_stream_closed(
                                    seam.last_write_error() or OSError("downstream closed")
                                )
                except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                    return finish_converted_sse_semantic_error(exc)
                except UpstreamProtocolTranslationError:
                    return finish_converted_sse_semantic_error(
                        _verified_converted_sse_semantic_error("responses")
                    )
                except UpstreamStreamIncompleteError:
                    incomplete_frame = True
                except UpstreamStreamIdleTimeoutError as exc:
                    self.close_connection = True
                    idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_idle_timeout",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        stream_idle_timeout_seconds=exc.timeout_seconds,
                        stream_idle_phase=exc.phase,
                        detail=idle_detail,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=idle_detail,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except DownstreamKeepaliveFailedError:
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    self.close_connection = True
                    stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=stream_detail,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                if incomplete_frame or not converter.completed:
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_incomplete",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream stream ended before response.completed.",
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502
                if not self._write_sse_done():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                self.close_connection = True
                _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
                return status

            if (
                streaming_policy == StreamingPolicy.TRANSPARENT_CONVERTED
                and upstream_format == "chat_completions"
                and not want_chat_output
            ):
                line_ending = b"\n"
                converter = _ChatToResponsesStreamConverter()
                incomplete_frame = False
                try:
                    for frame in self._iter_upstream_sse_events(
                        response,
                        event_resets_idle_timeout=_chat_sse_event_resets_idle_timeout,
                        on_chunk=observe_diagnostic_sse_line,
                    ):
                        payload = _converted_sse_payload(
                            frame,
                            verified_source_format=verified_source_format,
                        )
                        if payload is None:
                            continue
                        events: list[dict[str, Any]] = []
                        if payload == "[DONE]":
                            events = converter.events_for_done()
                        else:
                            chat_error_detail = _redact_identity_in_text(
                                _chat_stream_error_detail(payload) or "",
                                relay_redact_identity,
                            )
                            if chat_error_detail:
                                write_proxy_event(
                                    "upstream_stream_error_event",
                                    request_id=request_id,
                                    model=model,
                                    upstream=upstream_name,
                                    status=502,
                                    upstream_format=upstream_format,
                                    inbound_format=inbound_format,
                                    error="chat_completions_error",
                                    detail=chat_error_detail,
                                )
                                if not self._write_downstream_sse_error(
                                    inbound_format=inbound_format,
                                    upstream_name=upstream_name,
                                    status=502,
                                    error="chat_completions_error",
                                    detail=chat_error_detail,
                                    redact_identity=relay_redact_identity,
                                ):
                                    return finish_downstream_stream_closed(
                                        seam.last_write_error() or OSError("downstream closed")
                                    )
                                _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                                return 502
                            _offer_usage_observed_sse_line(
                                usage_context,
                                frame.raw,
                                upstream_format=upstream_format,
                            )
                            events = converter.events_for_chunk(payload)
                        for event in events:
                            try:
                                if not self._write_sse_bytes(
                                    _sse_json_line(event, line_ending) + line_ending
                                ):
                                    return finish_downstream_stream_closed(
                                        seam.last_write_error() or OSError("downstream closed")
                                    )
                            except OSError as exc:
                                return finish_downstream_stream_closed(exc)
                except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                    return finish_converted_sse_semantic_error(exc)
                except UpstreamProtocolTranslationError:
                    return finish_converted_sse_semantic_error(
                        _verified_converted_sse_semantic_error(
                            "chat_completions"
                        )
                    )
                except UpstreamStreamIncompleteError:
                    incomplete_frame = True
                except UpstreamStreamIdleTimeoutError as exc:
                    self.close_connection = True
                    idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_idle_timeout",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        stream_idle_timeout_seconds=exc.timeout_seconds,
                        stream_idle_phase=exc.phase,
                        detail=idle_detail,
                    )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=idle_detail,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except DownstreamKeepaliveFailedError:
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    self.close_connection = True
                    stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=stream_detail,
                    )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                if (
                    not incomplete_frame
                    and not converter.completed
                    and converter.pending_incomplete is not None
                ):
                    for event in converter.events_for_done():
                        try:
                            if not self._write_sse_bytes(
                                _sse_json_line(event, line_ending) + line_ending
                            ):
                                return finish_downstream_stream_closed(
                                    seam.last_write_error() or OSError("downstream closed")
                                )
                        except OSError as exc:
                            return finish_downstream_stream_closed(exc)
                if incomplete_frame or not converter.completed:
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_incomplete",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                    )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream Chat Completions stream ended without finish_reason or [DONE].",
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502
                self.close_connection = True
                _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
                return status

            if want_chat_output and upstream_format != "chat_completions":
                # Upstream returns Responses SSE; convert to Chat Completions SSE.
                line_ending = b"\n"
                events: list[Mapping[str, Any]] = []
                incomplete_frame = False
                try:
                    for frame in self._iter_upstream_sse_events(
                        response,
                        event_resets_idle_timeout=_responses_sse_event_resets_idle_timeout,
                        on_chunk=observe_diagnostic_sse_line,
                    ):
                        line_ending = _sse_line_ending(frame.raw)
                        event = _converted_sse_payload(
                            frame,
                            verified_source_format=verified_source_format,
                        )
                        if event is None or event == "[DONE]":
                            continue
                        events.append(event)
                        if usage_policy == UsagePolicy.ASYNC_TAP:
                            _offer_usage_observed_sse_line(
                                usage_context,
                                frame.raw,
                                upstream_format=upstream_format,
                            )
                        else:
                            _capture_usage(usage_capture, _usage_from_response_event(event))
                except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                    return finish_converted_sse_semantic_error(exc)
                except UpstreamStreamIncompleteError:
                    incomplete_frame = True
                except UpstreamStreamIdleTimeoutError as exc:
                    if defer_stream_errors:
                        raise
                    self.close_connection = True
                    idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_idle_timeout",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        stream_idle_timeout_seconds=exc.timeout_seconds,
                        stream_idle_phase=exc.phase,
                        detail=idle_detail,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=idle_detail,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except DownstreamKeepaliveFailedError:
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    self.close_connection = True
                    stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=stream_detail,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                try:
                    if incomplete_frame:
                        raise UpstreamStreamIncompleteError(
                            "Upstream SSE stream ended with an incomplete pending frame"
                        )
                    response_body = compatible_response_body(
                        _events_to_responses_body(events, require_completed=True),
                        upstream_name,
                        event_context=compatibility_event_context,
                    )
                except UpstreamStreamIncompleteError:
                    if defer_stream_errors:
                        raise
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_incomplete",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream stream ended before response.completed.",
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502

                try:
                    converted_chat_chunks = _chat_completion_body_to_stream_chunks(
                        _response_body_to_chat_completion_body(response_body)
                    )
                except UpstreamProtocolTranslationError:
                    if verified_source_format is None:
                        raise
                    return finish_converted_sse_semantic_error(
                        _verified_converted_sse_semantic_error(
                            verified_source_format
                        )
                    )

                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                for chunk in converted_chat_chunks:
                    if not self._write_sse_data(chunk):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                if not self._write_sse_done():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                self.close_connection = True
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="async_usage_pending"
                    if usage_policy == UsagePolicy.ASYNC_TAP
                    else "upstream_missing_usage",
                )
                return status

            if upstream_format == "chat_completions":
                line_ending = b"\n"
                chunks: list[Mapping[str, Any] | str] = []
                incomplete_frame = False
                try:
                    for frame in self._iter_upstream_sse_events(
                        response,
                        event_resets_idle_timeout=_chat_sse_event_resets_idle_timeout,
                        on_chunk=observe_diagnostic_sse_line,
                    ):
                        line_ending = _sse_line_ending(frame.raw)
                        payload = _converted_sse_payload(
                            frame,
                            verified_source_format=verified_source_format,
                        )
                        if payload is None:
                            continue
                        if payload == "[DONE]":
                            chunks.append("[DONE]")
                            continue
                        chunks.append(payload)
                        if usage_policy == UsagePolicy.ASYNC_TAP:
                            _offer_usage_observed_sse_line(
                                usage_context,
                                frame.raw,
                                upstream_format=upstream_format,
                            )
                        else:
                            _capture_usage(usage_capture, _usage_from_payload(payload))
                except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                    return finish_converted_sse_semantic_error(exc)
                except UpstreamStreamIncompleteError:
                    incomplete_frame = True
                except UpstreamStreamIdleTimeoutError as exc:
                    if defer_stream_errors:
                        raise
                    self.close_connection = True
                    idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_idle_timeout",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        stream_idle_timeout_seconds=exc.timeout_seconds,
                        stream_idle_phase=exc.phase,
                        detail=idle_detail,
                    )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=idle_detail,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except DownstreamKeepaliveFailedError:
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    self.close_connection = True
                    stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=stream_detail,
                    )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                if incomplete_frame or not _chat_stream_chunks_have_terminal(chunks):
                    if defer_stream_errors:
                        raise UpstreamStreamIncompleteError(
                            "Chat Completions stream ended without finish_reason or [DONE]"
                        )
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_incomplete",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                    )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream Chat Completions stream ended without finish_reason or [DONE].",
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502
                if upstream_name != "official" and not want_chat_output:
                    chunks, _ = _suppress_chat_reasoning_extensions(
                        chunks,
                        event_context=event_context,
                        upstream_name=upstream_name,
                    )
                chat_summary = _chat_stream_shape_summary(chunks)
                _write_adapter_event(
                    event_context,
                    "chat_stream_shape_summary",
                    upstream=upstream_name,
                    inbound_format=inbound_format,
                    want_chat_output=want_chat_output,
                    **chat_summary,
                )
                lifecycle_issue = (
                    _chat_stream_lifecycle_final_issue(chunks, chat_summary, event_context, request_kind)
                    if status < 400
                    else None
                )
                if lifecycle_issue is not None:
                    _write_adapter_event(
                        event_context,
                        _lifecycle_final_issue_event_name(lifecycle_issue),
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        **chat_summary,
                    )
                    _capture_usage(
                        usage_capture,
                        None,
                        missing_reason=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                    )
                    _raise_lifecycle_final_issue(upstream_name, lifecycle_issue)
                if want_chat_output:
                    response_body = compatible_response_body(
                        _events_to_responses_body(_chat_stream_chunks_to_response_events(chunks)),
                        upstream_name,
                        event_context=compatibility_event_context,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    for chunk in _chat_completion_body_to_stream_chunks(
                        _response_body_to_chat_completion_body(response_body)
                    ):
                        if not self._write_sse_data(chunk):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                else:
                    events = _chat_stream_chunks_to_response_events(chunks)
                    runtime_tool_plan, runtime_tool_stream = (
                        _runtime_tool_compatibility_stream_for_attempt(
                            compatibility_event_context
                        )
                    )
                    if runtime_tool_plan is not None and runtime_tool_stream is not None:
                        decoded_events: list[Mapping[str, Any]] = []
                        try:
                            for event in events:
                                decoded_events.extend(
                                    runtime_tool_stream.decode_events_for_event(event)
                                )
                        except RuntimeToolCompatibilityError as exc:
                            _raise_runtime_tool_compatibility_error(exc)
                        events = decoded_events
                    _write_adapter_event(
                        event_context,
                        "chat_to_responses_event_summary",
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        stage="converted",
                        **_response_events_shape_summary(events),
                    )
                    events, _ = _repair_missing_required_subagent_call_events(events, event_context)
                    events, _ = _adapt_third_party_apply_patch_stream_events(
                        events,
                        event_context=compatibility_event_context,
                    )
                    events, _ = _normalize_third_party_tool_call(events, compatibility_event_context)
                    events, _ = _suppress_bounded_tool_search_calls(
                        events,
                        compatibility_event_context,
                    )
                    _write_adapter_event(
                        event_context,
                        "chat_to_responses_event_summary",
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        stage="normalized",
                        **_response_events_shape_summary(events),
                    )
                    events, _ = _suppress_worker_multi_agent_tool_calls(events, event_context)
                    events, _ = _suppress_coordinator_forbidden_tool_calls(events, event_context)
                    events, _ = _downgrade_invalid_third_party_tool_calls(events)
                    _write_adapter_event(
                        event_context,
                        "chat_to_responses_event_summary",
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        stage="downgraded",
                        **_response_events_shape_summary(events),
                    )
                    events, _ = _guard_duplicate_multi_agent_spawn_calls(events, event_context)
                    events, _ = _apply_external_worker_response_contract(
                        events,
                        compatibility_event_context,
                        surface="sse",
                        attach_sidecars=False,
                    )
                    events, _ = _coerce_exact_spawn_prompt_tool_calls(events, event_context)
                    events, _ = _coerce_required_subagent_tool_calls(
                        events,
                        event_context,
                        surface="sse",
                    )
                    events, _ = _reconcile_function_call_argument_events(events)
                    events, _ = _repair_missing_required_subagent_call_events(events, event_context)
                    events, _ = _apply_external_worker_response_contract(
                        events,
                        compatibility_event_context,
                        surface="sse",
                        validate_selectors=False,
                    )
                    _write_adapter_event(
                        event_context,
                        "chat_to_responses_event_summary",
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        stage="final",
                        **_response_events_shape_summary(events),
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    for event in events:
                        if not self._write_sse_bytes(
                            _sse_json_line(event, line_ending) + line_ending
                        ):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                if not self._write_sse_done():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                self.close_connection = True
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="async_usage_pending"
                    if usage_policy == UsagePolicy.ASYNC_TAP
                    else "upstream_missing_usage",
                )
                return status

            if lifecycle_final_retry_enabled:
                reasoning_stats: dict[str, Any] = {
                    "seen": False,
                    "original_event_counts": {},
                    "rewritten_event_counts": {},
                    "delta_events": 0,
                    "delta_chars": 0,
                }
                saw_response_event = False
                saw_terminal_event = False
                downstream_output_started = False
                buffered_lines: list[tuple[bytes, bool]] = []
                rewritten_events: list[Mapping[str, Any]] = []
                apply_patch_stream_adapter = (
                    _ThirdPartyApplyPatchStreamAdapter(compatibility_event_context)
                    if (
                        upstream_name != "official"
                        and not want_chat_output
                        and _apply_patch_adapter_enabled(compatibility_event_context)
                    )
                    else None
                )
                try:
                    for line in self._iter_upstream_sse_lines(
                        response,
                        line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
                        on_line=observe_diagnostic_sse_line,
                    ):
                        if not line:
                            break
                        original_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                        usage_payload = _parse_sse_json_payload(line)
                        if isinstance(usage_payload, Mapping):
                            remember_response_id(usage_payload)
                            event_type = usage_payload.get("type")
                            if isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error"):
                                saw_response_event = True
                            if _responses_events_have_terminal([usage_payload]):
                                saw_terminal_event = True
                            if _responses_event_starts_downstream_output(usage_payload):
                                downstream_output_started = True
                                if seam is not None:
                                    seam.mark_downstream_content_exposed()
                            _capture_usage(usage_capture, _usage_from_response_event(usage_payload))
                        rewritten_line = line
                        if apply_patch_stream_adapter is not None and isinstance(usage_payload, Mapping):
                            replacement_events, apply_patch_changed = apply_patch_stream_adapter.events_for_event(
                                usage_payload
                            )
                            if apply_patch_changed:
                                rewritten_line = (
                                    _sse_json_line(replacement_events[0], _sse_line_ending(line))
                                    if replacement_events
                                    else b""
                                )
                        rewritten_line = compatible_sse_line(
                            rewritten_line,
                            upstream_name,
                            event_context=compatibility_event_context,
                        )
                        rewritten_payloads = (
                            _parse_sse_json_payloads(rewritten_line)
                            if upstream_name != "official"
                            else ([usage_payload] if isinstance(usage_payload, Mapping) else [])
                        )
                        if rewritten_payloads:
                            _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payloads[0])
                            for emitted_payload in rewritten_payloads[1:]:
                                _count_sse_reasoning_event(reasoning_stats, None, emitted_payload)
                            rewritten_events.extend(rewritten_payloads)
                        else:
                            _count_sse_reasoning_event(reasoning_stats, original_payload, None)
                        terminal = _responses_events_have_terminal(rewritten_payloads)
                        buffered_lines.append((rewritten_line, terminal))
                        if saw_terminal_event:
                            break
                except UpstreamStreamIdleTimeoutError as exc:
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_idle_timeout",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        stream_idle_timeout_seconds=exc.timeout_seconds,
                        stream_idle_phase=exc.phase,
                        terminal_seen=saw_terminal_event,
                        downstream_output_started=downstream_output_started,
                        detail=safe_upstream_error_detail(exc, redact_identity=relay_redact_identity),
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=idle_detail,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except DownstreamKeepaliveFailedError:
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    self.close_connection = True
                    stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=stream_detail,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                if apply_patch_stream_adapter is not None and saw_terminal_event:
                    apply_patch_stream_adapter.finish()
                if status < 400 and saw_response_event and not saw_terminal_event:
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_incomplete",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                    )
                    if not send_downstream_response_headers_once():
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if not self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream Responses stream ended without a terminal event.",
                        redact_identity=relay_redact_identity,
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502
                lifecycle_issue = (
                    _responses_events_lifecycle_final_issue(rewritten_events, event_context, request_kind)
                    if status < 400
                    else None
                )
                if lifecycle_issue is not None:
                    _write_adapter_event(
                        event_context,
                        _lifecycle_final_issue_event_name(lifecycle_issue),
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        **_response_events_shape_summary(list(rewritten_events)),
                    )
                    _capture_usage(
                        usage_capture,
                        None,
                        missing_reason=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                    )
                    _raise_lifecycle_final_issue(upstream_name, lifecycle_issue)
                if upstream_name != "official" and reasoning_stats["seen"]:
                    write_proxy_event(
                        "sse_reasoning_summary",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        original_event_counts=reasoning_stats["original_event_counts"],
                        rewritten_event_counts=reasoning_stats["rewritten_event_counts"],
                        delta_events=reasoning_stats["delta_events"],
                        delta_chars=reasoning_stats["delta_chars"],
                    )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                for buffered_line, terminal in buffered_lines:
                    if not self._write_sse_bytes(buffered_line):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    if terminal:
                        separator = _sse_event_separator_after_line(buffered_line)
                        if separator:
                            if not self._write_sse_bytes(separator):
                                return finish_downstream_stream_closed(
                                    seam.last_write_error() or OSError("downstream closed")
                                )
                    if terminal:
                        break
                self.close_connection = True
                _capture_usage(usage_capture, None)
                return status

            reasoning_stats: dict[str, Any] = {
                "seen": False,
                "original_event_counts": {},
                "rewritten_event_counts": {},
                "delta_events": 0,
                "delta_chars": 0,
            }
            saw_terminal_event = False
            saw_completed_event = False
            visible_or_tool_output_seen = False
            downstream_output_started = False
            pending_sse_event_metadata: list[bytes] = []
            pending_downstream_lines: list[bytes] = []
            drop_next_sse_separator = False
            created_response: dict[str, Any] | None = None
            completed_tool_output_items: list[dict[str, Any]] = []
            last_response_event_type: str | None = None
            apply_patch_stream_adapter = (
                _ThirdPartyApplyPatchStreamAdapter(compatibility_event_context)
                if (
                    upstream_name != "official"
                    and not want_chat_output
                    and _apply_patch_adapter_enabled(compatibility_event_context)
                )
                else None
            )

            class DownstreamWriteFailedError(Exception):
                """Raised when a required downstream SSE commit fails."""

            def write_or_queue_downstream_line(out_line: bytes, *, buffer: bool = False, force: bool = False) -> None:
                if not out_line:
                    return
                if buffer and not force:
                    pending_downstream_lines.append(out_line)
                    return
                if pending_downstream_lines:
                    for pending_line in pending_downstream_lines:
                        if not self._write_sse_bytes(pending_line):
                            raise DownstreamWriteFailedError()
                    pending_downstream_lines.clear()
                if not self._write_sse_bytes(out_line):
                    raise DownstreamWriteFailedError()

            def flush_pending_downstream_lines() -> None:
                if not pending_downstream_lines:
                    return
                for pending_line in pending_downstream_lines:
                    if not self._write_sse_bytes(pending_line):
                        raise DownstreamWriteFailedError()
                pending_downstream_lines.clear()

            def write_response_failed_event(error_payload: Mapping[str, Any]) -> None:
                pending_downstream_lines.clear()
                error_value = error_payload.get("error")
                if isinstance(error_value, Mapping):
                    sanitized_error: dict[str, Any] = {
                        key: _redact_identity_in_text(str(value), relay_redact_identity)
                        for key, value in error_value.items()
                    }
                else:
                    sanitized_error = {
                        "message": _redact_identity_in_text(
                            str(error_value or "Upstream stream error"),
                            relay_redact_identity,
                        )
                    }
                response_payload = {
                    "id": f"resp_{uuid.uuid4().hex[:12]}",
                    "object": "response",
                    "status": "failed",
                    "model": model,
                    "output": [],
                    "error": sanitized_error,
                }
                if not self._write_sse_event(
                    "response.failed",
                    {"type": "response.failed", "response": response_payload},
                ):
                    raise DownstreamWriteFailedError()

            def remember_completed_tool_event(payload: Mapping[str, Any]) -> None:
                nonlocal created_response
                event_type = payload.get("type")
                if event_type == "response.created":
                    response_payload = payload.get("response")
                    if isinstance(response_payload, Mapping):
                        created_response = dict(response_payload)
                    return
                if event_type != "response.output_item.done":
                    return
                item = payload.get("item")
                if not isinstance(item, Mapping):
                    return
                completed = _responses_completed_tool_item(item)
                if completed is not None:
                    completed_tool_output_items.append(completed)

            def synthesize_completed_tool_response() -> bool:
                if upstream_name == "official" or downstream_output_started or not completed_tool_output_items:
                    return False
                event = _synthetic_response_completed_from_tool_items(
                    created_response=created_response,
                    model=model,
                    output_items=completed_tool_output_items,
                )
                if event is None:
                    return False
                pending_line_count = len(pending_downstream_lines)
                pending_byte_count = sum(len(pending_line) for pending_line in pending_downstream_lines)
                flush_pending_downstream_lines()
                if not self._write_sse_event("response.completed", event):
                    raise DownstreamWriteFailedError()
                write_proxy_event(
                    "upstream_stream_incomplete_synthesized_terminal",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=200,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    completed_tool_calls=len(completed_tool_output_items),
                    pending_downstream_lines=pending_line_count,
                    pending_downstream_bytes=pending_byte_count,
                    last_event_type=last_response_event_type,
                )
                return True

            try:
                for line in self._iter_upstream_sse_lines(
                    response,
                    line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
                    on_line=observe_diagnostic_sse_line,
                ):
                    if not line:
                        break
                    if upstream_name != "official" and _is_sse_blank_line(line):
                        if drop_next_sse_separator:
                            drop_next_sse_separator = False
                            pending_sse_event_metadata = []
                            continue
                        if pending_sse_event_metadata:
                            pending_sse_event_metadata = []
                            continue
                        write_or_queue_downstream_line(line, buffer=bool(pending_downstream_lines))
                        continue
                    if upstream_name != "official" and _is_sse_event_metadata_line(line):
                        pending_sse_event_metadata.append(line)
                        continue
                    original_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                    usage_payload = _parse_sse_json_payload(line)
                    buffer_current_line = False
                    if isinstance(usage_payload, Mapping):
                        remember_response_id(usage_payload)
                        event_type = usage_payload.get("type")
                        if isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error"):
                            last_response_event_type = event_type
                        if event_type == "error":
                            exc = UpstreamStreamErrorEvent(usage_payload)
                            if defer_stream_errors and not downstream_output_started:
                                pending_downstream_lines.clear()
                                pending_sse_event_metadata = []
                                raise exc
                            self.close_connection = True
                            stream_error_detail = safe_upstream_error_detail(
                                exc, redact_identity=relay_redact_identity
                            )
                            write_proxy_event(
                                "upstream_stream_error_event",
                                request_id=request_id,
                                model=model,
                                upstream=upstream_name,
                                status=502,
                                upstream_format=upstream_format,
                                inbound_format=inbound_format,
                                failure_class=_upstream_failure_class(exc),
                                detail=stream_error_detail,
                            )
                            write_response_failed_event(usage_payload)
                            _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                            return 502
                        if _responses_events_have_terminal([usage_payload]):
                            if not saw_terminal_event:
                                _observe_gateway_diagnostic(
                                    "observe_terminal",
                                    request_id,
                                    forwarded=False,
                                )
                            saw_terminal_event = True
                        if event_type == "response.completed":
                            saw_completed_event = True
                        if _responses_event_has_visible_or_tool_output(usage_payload, upstream_name):
                            visible_or_tool_output_seen = True
                            if seam is not None:
                                seam.mark_downstream_content_exposed()
                        empty_completed_candidate = (
                            upstream_name != "official"
                            and event_type == "response.completed"
                            and not visible_or_tool_output_seen
                        )
                        is_tool_construction = _responses_event_is_tool_call_construction(usage_payload)
                        if (
                            is_tool_construction
                            and not downstream_output_started
                            and not saw_terminal_event
                        ):
                            buffer_current_line = True
                        else:
                            item = usage_payload.get("item") if event_type == "response.output_item.done" else None
                            is_reasoning_done = isinstance(item, Mapping) and item.get("type") == "reasoning"
                            if (
                                _responses_event_commits_downstream_output(usage_payload, upstream_name)
                                and (
                                    upstream_name == "official"
                                    or is_reasoning_done
                                    or _is_reasoning_summary_stream_event(usage_payload)
                                )
                            ):
                                downstream_output_started = True
                        buffer_current_line = (
                            buffer_current_line
                            or empty_completed_candidate
                            or not downstream_output_started
                            and not saw_terminal_event
                        )
                        _capture_usage(usage_capture, _usage_from_response_event(usage_payload))
                    elif (
                        pending_downstream_lines
                        and not downstream_output_started
                        and not saw_terminal_event
                    ):
                        buffer_current_line = True
                    if apply_patch_stream_adapter is not None and isinstance(usage_payload, Mapping):
                        replacement_events, apply_patch_changed = apply_patch_stream_adapter.events_for_event(usage_payload)
                        if apply_patch_changed:
                            if not replacement_events:
                                line = b""
                            else:
                                line = _sse_json_line(replacement_events[0], _sse_line_ending(line))
                    line = compatible_sse_line(line, upstream_name, event_context=compatibility_event_context)
                    rewritten_payloads = (
                        _parse_sse_json_payloads(line)
                        if upstream_name != "official"
                        else []
                    )
                    if rewritten_payloads:
                        for emitted_payload in rewritten_payloads:
                            remember_completed_tool_event(emitted_payload)
                        _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payloads[0])
                        for emitted_payload in rewritten_payloads[1:]:
                            _count_sse_reasoning_event(reasoning_stats, None, emitted_payload)
                    elif isinstance(usage_payload, Mapping):
                        remember_completed_tool_event(usage_payload)
                        _count_sse_reasoning_event(reasoning_stats, original_payload, None)
                    else:
                        _count_sse_reasoning_event(reasoning_stats, original_payload, None)

                    if not line and upstream_name != "official":
                        pending_sse_event_metadata = []
                        drop_next_sse_separator = True
                        continue

                    if pending_sse_event_metadata:
                        for metadata_line in pending_sse_event_metadata:
                            write_or_queue_downstream_line(metadata_line, buffer=buffer_current_line)
                        pending_sse_event_metadata = []
                    write_or_queue_downstream_line(line, buffer=buffer_current_line)
                    if saw_terminal_event:
                        separator = _sse_event_separator_after_line(line)
                        if separator:
                            flush_terminal = not (
                                upstream_name != "official"
                                and isinstance(usage_payload, Mapping)
                                and usage_payload.get("type") == "response.completed"
                                and not visible_or_tool_output_seen
                            )
                            write_or_queue_downstream_line(
                                separator,
                                buffer=not flush_terminal,
                                force=flush_terminal,
                            )
                            if flush_terminal:
                                _observe_gateway_diagnostic(
                                    "observe_terminal",
                                    request_id,
                                    forwarded=True,
                                )
                    if saw_terminal_event:
                        break
            except UpstreamStreamIdleTimeoutError as exc:
                if defer_stream_errors and not downstream_output_started:
                    raise
                self.close_connection = True
                idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_idle_timeout",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    stream_idle_timeout_seconds=exc.timeout_seconds,
                    stream_idle_phase=exc.phase,
                    terminal_seen=saw_terminal_event,
                    downstream_output_started=downstream_output_started,
                    detail=idle_detail,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_idle_timeout",
                    detail=idle_detail,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                return 502
            except DownstreamKeepaliveFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if defer_stream_errors and not downstream_output_started:
                    raise UpstreamStreamInterruptedError(exc) from exc
                self.close_connection = True
                stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_interrupted",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    error=type(exc).__name__,
                    detail=stream_detail,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                return 502
            except DownstreamWriteFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            if status < 400 and not saw_terminal_event:
                try:
                    synthesized_terminal = synthesize_completed_tool_response()
                except DownstreamWriteFailedError:
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if synthesized_terminal:
                    if apply_patch_stream_adapter is not None:
                        apply_patch_stream_adapter.finish(allow_missing_terminal=True)
                    self.close_connection = True
                    _capture_usage(usage_capture, None, missing_reason="synthetic_tool_terminal")
                    return status
                if defer_stream_errors and not downstream_output_started:
                    raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")
                self.close_connection = True
                retry_forbidden = bool(
                    downstream_output_started
                    or completed_tool_output_items
                    or seam._downstream_content_exposed
                )
                write_proxy_event(
                    "upstream_stream_incomplete",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    terminal_seen=saw_terminal_event,
                    downstream_output_started=downstream_output_started,
                    terminal=False,
                    failure_class=RETRY_FAILURE_QUICK_TRANSIENT,
                    failure_phase="stream_body",
                    failure_side="upstream_read",
                    retry_forbidden=retry_forbidden,
                    retry_safety_class=(
                        RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
                        if retry_forbidden
                        else RETRY_SAFETY_SUPPRESSED_POST_WRITE
                    ),
                    completed_tool_calls=len(completed_tool_output_items),
                    pending_downstream_lines=len(pending_downstream_lines),
                    pending_downstream_bytes=sum(len(pending_line) for pending_line in pending_downstream_lines),
                    last_event_type=last_response_event_type,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream Responses stream ended without a terminal event.",
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                return 502
            if apply_patch_stream_adapter is not None:
                apply_patch_stream_adapter.finish()
            if (
                status < 400
                and upstream_name != "official"
                and saw_completed_event
                and not visible_or_tool_output_seen
            ):
                pending_line_count = len(pending_downstream_lines)
                pending_byte_count = sum(len(pending_line) for pending_line in pending_downstream_lines)
                pending_downstream_lines.clear()
                detail = "Upstream Responses stream completed without visible output or tool calls."
                if defer_stream_errors:
                    raise UpstreamEmptyCompletedResponseError(
                        f"Responses stream returned empty completed response: {detail}"
                    )
                self.close_connection = True
                write_proxy_event(
                    "upstream_empty_completed_response",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    terminal_seen=saw_terminal_event,
                    completed_seen=saw_completed_event,
                    visible_or_tool_output_seen=visible_or_tool_output_seen,
                    completed_tool_calls=len(completed_tool_output_items),
                    pending_downstream_lines=pending_line_count,
                    pending_downstream_bytes=pending_byte_count,
                    last_event_type=last_response_event_type,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_empty_completed_response",
                    detail=detail,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="empty_completed_response")
                return 502
            if upstream_name != "official" and reasoning_stats["seen"]:
                write_proxy_event(
                    "sse_reasoning_summary",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    original_event_counts=reasoning_stats["original_event_counts"],
                    rewritten_event_counts=reasoning_stats["rewritten_event_counts"],
                    delta_events=reasoning_stats["delta_events"],
                    delta_chars=reasoning_stats["delta_chars"],
                )
            self.close_connection = True
            _capture_usage(
                usage_capture,
                None,
                missing_reason="async_usage_pending"
                if usage_policy == UsagePolicy.ASYNC_TAP
                else "upstream_missing_usage",
            )
            return status

        if not self._write_non_streaming_body_relay(body):
            return finish_downstream_stream_closed(
                seam.last_write_error() or OSError("downstream closed")
            )
        self.close_connection = True
        _capture_usage(
            usage_capture,
            None,
            missing_reason="async_usage_pending"
            if usage_policy == UsagePolicy.ASYNC_TAP
            else "upstream_missing_usage",
        )
        return status

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)


def run_server(host: str, port: int) -> None:
    PROXY_TEXT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(PROXY_TEXT_LOG_PATH, encoding="utf-8"),
        ],
        force=True,
    )
    server = ThreadingHTTPServer((host, port), CodexProxyHandler)
    server.daemon_threads = True
    shutdown_controller = GatewayShutdownController()
    server.gateway_shutdown_controller = shutdown_controller
    logger.info("serving Codex proxy on %s:%s", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if shutdown_controller.shutdown_requested:
            shutdown_controller.wait_for_active_requests()
            flush_timeout = min(
                GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS,
                shutdown_controller.remaining_shutdown_budget_seconds(),
            )
        else:
            flush_timeout = GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS
        writer_result = GATEWAY_EVENT_WRITER.shutdown(
            timeout=flush_timeout,
        )
        if not writer_result.completed:
            logger.warning("Gateway event writer shutdown ended with %s", writer_result.outcome)
        try:
            diagnostic_shutdown = getattr(GATEWAY_DIAGNOSTIC_RECORDER, "shutdown", None)
            diagnostic_timeout = (
                min(
                    GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS,
                    shutdown_controller.remaining_shutdown_budget_seconds(),
                )
                if shutdown_controller.shutdown_requested
                else GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS
            )
            if callable(diagnostic_shutdown) and not diagnostic_shutdown(diagnostic_timeout):
                logger.warning("Gateway diagnostic recorder shutdown did not drain")
        except Exception:
            logger.warning("Gateway diagnostic recorder shutdown failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex model routing proxy.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
