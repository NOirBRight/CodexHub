from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
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
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import tomllib
from typing import Any, Callable, Mapping
import uuid
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
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

from codex_semantic_adapter import (
    coerce_number as _semantic_coerce_number,
    coerce_target as _semantic_coerce_target,
    coerce_targets as _semantic_coerce_targets,
    multi_agent_discovery_arguments as _semantic_multi_agent_discovery_arguments,
    normalize_multi_agent_arguments as _semantic_normalize_multi_agent_arguments,
    normalize_tool_search_arguments as _semantic_normalize_tool_search_arguments,
)

from catalog import (
    CatalogPolicy,
    canonical_model_id,
    deny_match_model_id,
    load_catalog_models,
    load_policy,
    should_include_external_provider_model,
    should_include_model,
)
from catalog_sync import (
    GENERATED_CATALOG_PATH,
    POLICY_PATH,
    existing_generated_catalog_path,
    known_official_model_ids as catalog_known_official_model_ids,
    official_short_display_name,
    sync_catalog,
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
        idle_seconds = time.monotonic() - released_at if isinstance(released_at, (int, float)) else None
        max_idle_seconds = (
            OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS if self.proxy is not None else OFFICIAL_POOL_MAX_IDLE_SECONDS
        )
        if idle_seconds is not None and idle_seconds >= max_idle_seconds:
            connection.close()
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
        raise translated from exc

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
    "deepseek-v4-pro": 65536,
    "deepseek-v4-flash": 65536,
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
                        "agent_type": {"type": "string"},
                        "fork_context": {"type": "boolean"},
                        "message": {"type": "string"},
                    },
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
PROXY_EVENT_LOG_PATH = RUNTIME_PROXY_DIR / "codex-proxy-events.jsonl"
PROXY_TEXT_LOG_PATH = RUNTIME_PROXY_DIR / "codex-proxy.log"
PROXY_EVENT_LOG_LOCK = threading.Lock()
PROXY_EVENT_QUEUE_MAXSIZE = _env_positive_int("CODEX_PROXY_EVENT_QUEUE_MAXSIZE", 4096)
PROXY_EVENT_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=PROXY_EVENT_QUEUE_MAXSIZE)
PROXY_EVENT_WRITER_LOCK = threading.Lock()
PROXY_EVENT_WRITER_THREAD: threading.Thread | None = None
PROXY_EVENT_DROPPED_COUNT = 0
PROXY_EVENT_DROPPED_LOCK = threading.Lock()
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300
DEFAULT_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS = 600.0
DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS = DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS
DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS = DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS
DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS = 3
DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS = 30
OPENAI_CONTEXT_GUARD_CONTEXT_WINDOW = 272_000
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
    """Raised when an image proxy request cannot be prepared safely."""


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


class UpstreamStreamIncompleteError(RuntimeError):
    """Raised when an upstream stream ends without a terminal event."""


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
    return value if value > 0 else DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS


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


def openai_context_guard_enabled() -> bool:
    return _env_or_settings_flag(
        "CODEX_PROXY_OPENAI_CONTEXT_GUARD_ENABLED",
        "openai_context_guard_enabled",
        False,
    )


def gateway_transparent_vision_proxy_enabled() -> bool:
    settings_value = _runtime_settings_value("gateway_transparent_vision_proxy_enabled")
    if isinstance(settings_value, bool):
        return settings_value
    if isinstance(settings_value, str):
        return settings_value.strip().lower() not in {"0", "false", "no", "off", ""}
    raw_value = os.environ.get("CODEX_PROXY_TRANSPARENT_VISION_PROXY_ENABLED")
    if raw_value is not None:
        return raw_value.strip().lower() not in {"0", "false", "no", "off", ""}
    return gateway_image_proxy_enabled()


def write_proxy_event(event: str, **fields: Any) -> None:
    payload = proxy_telemetry.prepare_event_payload(event, fields, RUNTIME_CODEX_DIR)
    _enqueue_proxy_event_payload(payload)


def _enqueue_proxy_event_payload(payload: dict[str, Any]) -> bool:
    global PROXY_EVENT_DROPPED_COUNT
    _ensure_proxy_event_writer_started()
    try:
        PROXY_EVENT_QUEUE.put_nowait(payload)
        return True
    except queue.Full:
        with PROXY_EVENT_DROPPED_LOCK:
            PROXY_EVENT_DROPPED_COUNT += 1
        return False


def _ensure_proxy_event_writer_started() -> None:
    global PROXY_EVENT_WRITER_THREAD
    with PROXY_EVENT_WRITER_LOCK:
        if PROXY_EVENT_WRITER_THREAD is not None and PROXY_EVENT_WRITER_THREAD.is_alive():
            return
        PROXY_EVENT_WRITER_THREAD = threading.Thread(
            target=_proxy_event_writer_loop,
            name="codex-proxy-event-writer",
            daemon=True,
        )
        PROXY_EVENT_WRITER_THREAD.start()


def _proxy_event_writer_loop() -> None:
    while True:
        payload = PROXY_EVENT_QUEUE.get()
        try:
            _write_proxy_event_payload_to_log(payload)
        finally:
            PROXY_EVENT_QUEUE.task_done()


def _write_proxy_event_payload_to_log(payload: Mapping[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    try:
        with PROXY_EVENT_LOG_LOCK:
            PROXY_EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with PROXY_EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
    except OSError as exc:
        logger.warning("failed to write proxy event log: %s", type(exc).__name__)


def flush_proxy_event_writer(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while PROXY_EVENT_QUEUE.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


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


def _usage_observed_context(
    event_context: Mapping[str, Any] | None,
    *,
    request_id: str | None,
    model: str | None,
    upstream: str,
    upstream_format: str,
    inbound_format: str,
) -> dict[str, Any]:
    context = {
        key: value
        for key, value in (event_context or {}).items()
        if not str(key).startswith("_")
    }
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
        OFFICIAL_PASSTHROUGH_USAGE_QUEUE.put_nowait((dict(context), line))
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
        USAGE_OBSERVED_QUEUE.put_nowait(("body", dict(context), body, None))
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
        USAGE_OBSERVED_QUEUE.put_nowait(("sse", dict(context), line, upstream_format))
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
    payload = {key: value for key, value in event_context.items() if not str(key).startswith("_")}
    payload.update(fields)
    write_proxy_event(event, **payload)


def _event_context_with_request_kind(context: Mapping[str, Any], request_kind: str) -> dict[str, Any]:
    payload = dict(context)
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
    return {
        canonical_model_id(str(model["slug"]))
        for model in load_catalog_models(existing_generated_catalog_path(path))
        if model.get("slug")
    }


def generated_catalog_by_slug(path: Path = GENERATED_CATALOG_PATH) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    for model in load_catalog_models(existing_generated_catalog_path(path)):
        slug = canonical_model_id(str(model.get("slug", "")))
        if slug:
            models[slug] = model
    return models


def catalog_max_output_tokens(model_id: str) -> int | None:
    slug = canonical_model_id(model_id)
    model = generated_catalog_by_slug().get(slug)
    if not model:
        cap = UPSTREAM_MAX_OUTPUT_TOKEN_CAPS.get(slug)
        return cap if isinstance(cap, int) and cap > 0 else None
    value = model.get("max_output_tokens")
    catalog_value = value if isinstance(value, int) and value > 0 else None
    cap = UPSTREAM_MAX_OUTPUT_TOKEN_CAPS.get(slug)
    if isinstance(cap, int) and cap > 0:
        return min(catalog_value, cap) if catalog_value is not None else cap
    return catalog_value


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
    catalog = generated_catalog_by_slug()
    model = catalog.get(upstream_model) or catalog.get(alias)
    if not model or model.get("supported_in_api") is False:
        return None

    metadata = model.get("codex_proxy_metadata")
    if not isinstance(metadata, dict):
        return None
    catalog_upstream = canonical_model_id(str(metadata.get("upstream_model", "")))
    if (
        metadata.get("provider") != "openai"
        or metadata.get("upstream_name") != "official"
        or catalog_upstream != upstream_model
        or not catalog_upstream.startswith(official_prefixes())
    ):
        return None
    if policy_denies_any_model((slug, alias, catalog_upstream), policy):
        raise ValueError(f"model is not allowed: {slug}")
    return catalog_upstream


def official_alias_upstream_model(slug: str, policy: Any) -> str | None:
    if not slug.startswith(OFFICIAL_ALIAS_PREFIX):
        return None
    upstream_model = slug[len(OFFICIAL_ALIAS_PREFIX) :]
    if policy_denies_any_model((slug, upstream_model), policy):
        raise ValueError(f"model is not allowed: {slug}")
    if upstream_model.startswith(official_prefixes()) and should_include_model(upstream_model, policy):
        return upstream_model
    return None


def official_fast_variant_upstream_model(slug: str, policy: Any) -> str | None:
    fast_model = slug[len(OFFICIAL_ALIAS_PREFIX) :] if slug.startswith(OFFICIAL_ALIAS_PREFIX) else slug
    upstream_model = OFFICIAL_FAST_VARIANT_BASE_MODELS.get(fast_model)
    if upstream_model is None:
        return None
    upstream_alias = f"{OFFICIAL_ALIAS_PREFIX}{upstream_model}"
    if policy_denies_any_model((slug, fast_model, upstream_model, upstream_alias), policy):
        raise ValueError(f"model is not allowed: {slug}")
    if upstream_model.startswith(official_prefixes()) and should_include_model(upstream_model, policy):
        return upstream_model
    return None


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
    configured, runtime_model = resolve_ollama_cloud_model(model_id, require_api_key=False)
    if not configured:
        return None
    slug = canonical_model_id(model_id)
    if runtime_model is None:
        raise ValueError(f"model is not allowed: {slug}")

    policy_alias = runtime_model.get("alias", f"{OLLAMA_CLOUD_ALIAS_PREFIX}{slug}")
    upstream_model = runtime_model.get("upstream_model", slug)
    if policy_denies_any_model((slug, policy_alias, upstream_model), policy):
        raise ValueError(f"model is not allowed: {slug}")

    api_key = runtime_model.get("api_key")
    upstream: dict[str, Any] = {
        "name": "ollama_cloud",
        "base_url": runtime_model.get("base_url") or ollama_cloud_base_url(),
        "auth": "api_key" if api_key else "ollama_api_key",
        "upstream_model": upstream_model,
        "upstream_format": runtime_model.get("upstream_format", "responses"),
        "tool_protocol": runtime_model.get("tool_protocol", "auto"),
        "reports_cached_input_tokens": False,
        "input_modalities": tuple(runtime_model.get("input_modalities") or ("text",)),
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
        raise ValueError(f"model is not allowed: {slug}")

    runtime_upstream = ollama_cloud_runtime_upstream(slug, policy)
    if runtime_upstream is not None:
        return runtime_upstream

    if not (should_include_model(slug, policy) or should_include_model(upstream_model, policy)):
        raise ValueError(f"model is not allowed: {slug}")
    if upstream_model not in generated_catalog_slugs():
        raise ValueError(f"model is not in the generated cloud catalog: {upstream_model}")
    return {
        "name": "ollama_cloud",
        "base_url": ollama_cloud_base_url(),
        "auth": "ollama_api_key",
        "upstream_model": upstream_model,
        "reports_cached_input_tokens": False,
    }


def choose_upstream(model_id: str) -> dict[str, Any]:
    slug = canonical_model_id(str(model_id))
    if not slug:
        raise ValueError("model is required")

    policy = load_policy(POLICY_PATH)
    official_fast_variant = official_fast_variant_upstream_model(slug, policy)
    if official_fast_variant is not None:
        return {
            "name": "official",
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
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "upstream_model": official_alias,
            "reports_cached_input_tokens": True,
        }

    discovered_official = generated_official_catalog_upstream_model(slug, policy)
    if discovered_official is not None:
        return {
            "name": "official",
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "upstream_model": discovered_official,
            "reports_cached_input_tokens": True,
        }

    ollama_alias = ollama_cloud_alias_upstream_model(slug, policy)
    if ollama_alias is not None:
        return ollama_alias

    if slug.startswith(official_prefixes()):
        if not should_include_model(slug, policy):
            raise ValueError(f"model is not allowed: {slug}")
        return {
            "name": "official",
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "reports_cached_input_tokens": True,
        }

    external_model = resolve_external_model_alias(slug)
    if external_model is not None:
        policy_alias = external_model.get("alias", slug)
        if policy_denies_any_model((slug, policy_alias, external_model.get("matched_alias")), policy):
            raise ValueError(f"model is not allowed: {slug}")
        if not should_include_external_provider_model(policy_alias, policy):
            raise ValueError(f"model is not allowed: {slug}")
        return {
            "name": external_model["upstream_name"],
            "base_url": external_model["base_url"],
            "auth": "api_key",
            "api_key": external_model["api_key"],
            "upstream_model": external_model["upstream_model"],
            "upstream_format": external_model.get("upstream_format", "responses"),
            "tool_protocol": external_model.get("tool_protocol", "auto"),
            "reports_cached_input_tokens": bool(external_model.get("reports_cached_input_tokens")),
            "input_modalities": tuple(external_model.get("input_modalities") or ("text",)),
        }

    if "/" in slug:
        raise ValueError(f"external provider model is not configured: {slug}")

    runtime_ollama = ollama_cloud_runtime_upstream(slug, policy)
    if runtime_ollama is not None:
        return runtime_ollama

    if not should_include_model(slug, policy):
        raise ValueError(f"model is not allowed: {slug}")

    if slug in generated_catalog_slugs():
        return {
            "name": "ollama_cloud",
            "base_url": ollama_cloud_base_url(),
            "auth": "ollama_api_key",
            "reports_cached_input_tokens": False,
        }

    raise ValueError(f"model is not in the generated cloud catalog: {slug}")


def official_upstream() -> dict[str, Any]:
    return {
        "name": "official",
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
    "response.reasoning_summary_text.delta",
}
REASONING_TEXT_EVENT_PREFIXES = (
    "response.reasoning_text.",
    "response.reasoning_content.",
    "response.reasoning_raw_content.",
    "response.reasoning_summary_text.",
)


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
        if value.get("summary") != []:
            value["summary"] = []
            changed = True
        for key in ("content", "raw_content", "reasoning_content", "thinking", "encrypted_content"):
            if key in value:
                value.pop(key, None)
                changed = True

    for item in value.values():
        if _hide_reasoning_text(item):
            changed = True

    return changed


def _is_reasoning_text_stream_event(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get("type")
    return isinstance(event_type, str) and event_type.startswith(REASONING_TEXT_EVENT_PREFIXES)


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


SSE_EVENT_TYPE_TELEMETRY_LIMIT = 64


def _sse_field_value(line_without_ending: bytes, prefix: bytes) -> bytes:
    value = line_without_ending[len(prefix) :]
    if value.startswith(b" "):
        value = value[1:]
    return value


def _decode_sse_metadata_value(value: bytes) -> str | None:
    try:
        text = value.decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    return text or None


class PassthroughSseSemanticStats:
    def __init__(self) -> None:
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
        self._event_name: str | None = None
        self._data_lines: list[bytes] = []

    def observe_line(self, line: bytes) -> None:
        for physical_line in line.splitlines(keepends=True):
            self._observe_physical_line(physical_line)

    def finalize_pending(self) -> None:
        if self._event_name is not None or self._data_lines:
            self._finish_event()

    def has_pending_event(self) -> bool:
        return self._event_name is not None or bool(self._data_lines)

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
        }
        if self.last_event_type is not None:
            fields["sse_last_event_type"] = self.last_event_type
        if self.event_types_truncated:
            fields["sse_event_types_truncated"] = True
        return fields

    def _observe_physical_line(self, physical_line: bytes) -> None:
        line = physical_line
        for candidate in (b"\r\n", b"\n", b"\r"):
            if line.endswith(candidate):
                line = line[: -len(candidate)]
                break
        if line == b"":
            self._finish_event()
            return
        if line.startswith(b":"):
            return
        if line.startswith(b"event:"):
            self._event_name = _decode_sse_metadata_value(_sse_field_value(line, b"event:"))
            return
        if line.startswith(b"data:"):
            self._data_lines.append(_sse_field_value(line, b"data:"))

    def _finish_event(self) -> None:
        if self._event_name is None and not self._data_lines:
            return
        event_name = self._event_name
        data = b"\n".join(self._data_lines)
        self._event_name = None
        self._data_lines = []

        self.events_streamed += 1
        event_type = event_name
        payload: Any = None
        if data == b"[DONE]":
            self.done_sentinel_seen = True
            self.terminal_event_seen = True
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
        if event_type.startswith("response."):
            self.response_event_seen = True
        if event_type == "response.completed":
            self.completed_event_seen = True
        if event_type in RESPONSES_TERMINAL_EVENT_TYPES:
            self.terminal_event_seen = True

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
    if upstream_name == "official" and event_type == "response.reasoning_summary_text.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.output_item.done":
        item = event.get("item")
        return isinstance(item, Mapping) and item.get("type") == "reasoning"
    return False


def _responses_output_item_has_visible_or_tool_output(item: Mapping[str, Any]) -> bool:
    item_type = item.get("type")
    if item_type in {"function_call", "custom_tool_call"}:
        return _responses_completed_tool_item(item) is not None
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


def _chat_sse_line_resets_idle_timeout(line: bytes) -> bool:
    payload_bytes = _sse_payload_bytes(line)
    if payload_bytes is None:
        return False
    if payload_bytes == b"[DONE]":
        return True
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, Mapping)


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


def _responses_content_to_chat_content(value: Any) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""

    parts: list[dict[str, Any]] = []
    text_fragments: list[str] = []
    has_image = False
    for part in value:
        if not isinstance(part, Mapping):
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"} and isinstance(part.get("text"), str):
            text = part["text"]
            text_fragments.append(text)
            parts.append({"type": "text", "text": text})
            continue
        if part_type == "input_image" and isinstance(part.get("image_url"), str):
            has_image = True
            parts.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
            continue
        if part_type == "input_image" and isinstance(part.get("file_id"), str):
            has_image = True
            parts.append({"type": "text", "text": f"[Image file: {part['file_id']}]"})

    if has_image:
        return parts or [{"type": "text", "text": ""}]
    return "\n".join(fragment for fragment in text_fragments if fragment)


def _responses_input_to_chat_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list):
        return []

    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message" or (item_type is None and ("role" in item or "content" in item)):
            role = item.get("role")
            if role == "developer":
                role = "system"
            else:
                role = role if role in {"system", "user", "assistant"} else "user"
            content = _responses_content_to_chat_content(item.get("content"))
            messages.append({"role": role, "content": content})
            continue
        if item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=True, separators=(",", ":"))
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
            continue
        if item_type == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            output = item.get("output")
            content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=True, separators=(",", ":"))
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
    return messages


def _responses_tools_to_chat_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    tools: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        function: dict[str, Any] = {"name": name}
        description = item.get("description")
        if isinstance(description, str):
            function["description"] = description
        parameters = item.get("parameters")
        if isinstance(parameters, dict):
            function["parameters"] = parameters
        tools.append({"type": "function", "function": function})
    return tools


def _responses_tool_choice_to_chat_tool_choice(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if value.get("type") != "function":
        return value
    name = value.get("name")
    if not isinstance(name, str) or not name:
        return value
    return {"type": "function", "function": {"name": name}}


def _responses_request_to_chat_completion_body(body: bytes) -> bytes:
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return body

    messages: list[dict[str, str]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.extend(_responses_input_to_chat_messages(payload.get("input")))
    if not messages:
        messages.append({"role": "user", "content": ""})

    chat_payload: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
    }
    for key in ("stream", "temperature", "top_p", "presence_penalty", "frequency_penalty", "parallel_tool_calls"):
        if key in payload:
            chat_payload[key] = payload[key]
    if payload.get("stream") is True:
        stream_options = chat_payload.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        stream_options["include_usage"] = True
        chat_payload["stream_options"] = stream_options
    if "max_output_tokens" in payload:
        chat_payload["max_tokens"] = payload["max_output_tokens"]

    tools = _responses_tools_to_chat_tools(payload.get("tools"))
    if tools:
        chat_payload["tools"] = tools
    tool_choice = _responses_tool_choice_to_chat_tool_choice(payload.get("tool_choice"))
    if tool_choice is not None:
        chat_payload["tool_choice"] = tool_choice

    return json.dumps(chat_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _chat_completion_message_output(message: Mapping[str, Any], index: int) -> dict[str, Any] | None:
    content = message.get("content")
    text = content if isinstance(content, str) else _chat_content_text(content)
    if not text:
        return None
    return {
        "id": f"msg_{index}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


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


def _chat_completion_tool_outputs(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        content = message.get("content")
        text = content if isinstance(content, str) else _chat_content_text(content)
        return _xmlish_tool_call_outputs_from_text(text) if text else []
    output: list[dict[str, Any]] = []
    for index, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_{uuid.uuid4().hex[:12]}"
        arguments = function.get("arguments")
        output.append(
            {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": arguments if isinstance(arguments, str) else "",
            }
        )
    return output


def _chat_completion_to_response_body(body: bytes, *, repair: bool = True) -> bytes:
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return body

    output: list[dict[str, Any]] = []
    choices = payload.get("choices")
    if isinstance(choices, list):
        for index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            tool_outputs = _chat_completion_tool_outputs(message)
            if tool_outputs:
                output.extend(tool_outputs)
                continue
            message_output = _chat_completion_message_output(message, index)
            if message_output is not None:
                output.append(message_output)

    response_payload: dict[str, Any] = {
        "id": payload.get("id") if isinstance(payload.get("id"), str) else f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "status": "completed",
        "model": payload.get("model"),
        "output": output,
    }
    if "usage" in payload:
        response_payload["usage"] = payload["usage"]

    if repair:
        _hide_reasoning_text(response_payload)
        response_payload, _ = _normalize_third_party_tool_call(response_payload)
        response_payload, _ = _downgrade_invalid_third_party_tool_calls(response_payload)
    return json.dumps(response_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


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
    """Translate Chat Completions tool-call chunks into Responses events.

    OpenAI-compatible chat streams usually send tool_call.id only in the first
    delta. Later chunks may omit it or send an empty value, so the first
    non-empty id must win for Codex to pair tool calls with outputs.
    """
    states: dict[int, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    finished = False
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    model: str | None = None

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        chunk_model = chunk.get("model")
        if isinstance(chunk_model, str) and chunk_model:
            model = chunk_model
            break

    created_response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "status": "in_progress",
        "output": [],
    }
    if model:
        created_response["model"] = model
    events.append({"type": "response.created", "response": created_response})

    def state_for(index: int) -> dict[str, Any]:
        if index not in states:
            output_index = len(states)
            states[index] = {
                "output_index": output_index,
                "item_id": "",
                "call_id": "",
                "name": "",
                "arguments": [],
                "added": False,
            }
        return states[index]

    def maybe_emit_added(state: dict[str, Any]) -> None:
        if state["added"] or not state["call_id"] or not state["name"]:
            return
        state["item_id"] = f"fc_{state['call_id']}"
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": state["output_index"],
                "item": {
                    "id": state["item_id"],
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": state["call_id"],
                    "name": state["name"],
                    "arguments": "",
                },
            }
        )
        state["added"] = True

    for chunk in chunks:
        if chunk == "[DONE]":
            finished = True
            continue
        if not isinstance(chunk, Mapping):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") is not None:
                finished = True
            delta = choice.get("delta")
            message = choice.get("message")
            source = delta if isinstance(delta, dict) else message if isinstance(message, dict) else None
            if not isinstance(source, dict):
                continue
            content = source.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            tool_calls = source.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for fallback_index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                raw_index = tool_call.get("index", fallback_index)
                index = raw_index if isinstance(raw_index, int) else fallback_index
                state = state_for(index)

                call_id = tool_call.get("id")
                if isinstance(call_id, str) and call_id and not state["call_id"]:
                    state["call_id"] = call_id

                function = tool_call.get("function")
                if isinstance(function, dict):
                    name = function.get("name")
                    if isinstance(name, str) and name and not state["name"]:
                        state["name"] = _normalize_chat_function_call_name(name)
                    if state["name"] and not state["call_id"]:
                        state["call_id"] = f"call_{uuid.uuid4().hex[:12]}"
                    arguments = function.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        state["arguments"].append(arguments)

                maybe_emit_added(state)
                if state["added"] and isinstance(function, dict):
                    arguments = function.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        events.append(
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": state["item_id"],
                                "output_index": state["output_index"],
                                "delta": arguments,
                            }
                        )

    if finished:
        output: list[dict[str, Any]] = []
        text = "".join(text_parts)
        xmlish_tool_outputs = _xmlish_tool_call_outputs_from_text(text) if text else []
        for state in sorted(states.values(), key=lambda item: item["output_index"]):
            maybe_emit_added(state)
            if not state["added"]:
                continue
            arguments = "".join(state["arguments"])
            item = {
                "id": state["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": arguments,
            }
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": state["item_id"],
                    "output_index": state["output_index"],
                    "arguments": arguments,
                }
            )
            events.append({"type": "response.output_item.done", "output_index": state["output_index"], "item": item})
            output.append(item)
        if xmlish_tool_outputs and not output:
            for item in xmlish_tool_outputs:
                output_index = len(output)
                in_progress_item = dict(item)
                in_progress_item["status"] = "in_progress"
                in_progress_item["arguments"] = ""
                events.append(
                    {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": in_progress_item,
                    }
                )
                events.append(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "arguments": item["arguments"],
                    }
                )
                events.append({"type": "response.output_item.done", "output_index": output_index, "item": item})
                output.append(item)
        elif text and not output:
            output_index = len(output)
            item_id = f"msg_{uuid.uuid4().hex[:12]}"
            item = {
                "id": item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": {
                        "id": item_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                }
            )
            for part in text_parts:
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": 0,
                        "delta": part,
                    }
                )
            events.append(
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "text": text,
                }
            )
            events.append({"type": "response.output_item.done", "output_index": output_index, "item": item})
            output.append(item)
        completed_response: dict[str, Any] = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "output": output,
        }
        if model:
            completed_response["model"] = model
        events.append({"type": "response.completed", "response": completed_response})

    return events


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


def _chat_content_to_responses_content(value: Any) -> list[dict[str, Any]]:
    """Translate a chat-completions message ``content`` into Responses content parts."""
    if isinstance(value, str):
        return [{"type": "input_text", "text": value}]
    if not isinstance(value, list):
        return []
    parts: list[dict[str, Any]] = []
    for fragment in value:
        if not isinstance(fragment, dict):
            continue
        if fragment.get("type") == "text" and isinstance(fragment.get("text"), str):
            parts.append({"type": "input_text", "text": fragment["text"]})
        elif fragment.get("type") == "image_url" and isinstance(fragment.get("image_url"), dict):
            url = fragment["image_url"].get("url")
            if isinstance(url, str):
                parts.append({"type": "input_image", "image_url": url})
    return parts


def _chat_messages_to_responses_input(messages: Any) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert chat-completions ``messages`` into Responses ``instructions`` + ``input``.

    System messages are collected into ``instructions``; the rest become
    ``message`` input items in order.  Assistant messages with ``tool_calls``
    become ``function_call`` items so the upstream can reconstruct the transcript.
    """
    if not isinstance(messages, list):
        return None, []

    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            text = content if isinstance(content, str) else _chat_content_text(content)
            if text:
                instructions_parts.append(text)
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and role == "assistant":
            # Emit any textual content first, then function_call items.
            content = message.get("content")
            text = content if isinstance(content, str) else _chat_content_text(content)
            if text:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                })
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    continue
                call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                arguments = function.get("arguments")
                input_items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments if isinstance(arguments, str) else "",
                })
            continue
        if role == "tool":
            # tool result → function_call_output
            call_id = message.get("tool_call_id") or f"call_{uuid.uuid4().hex[:12]}"
            content = message.get("content")
            output = content if isinstance(content, str) else _chat_content_text(content)
            input_items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output or "",
            })
            continue
        # user / assistant text
        resp_role = role if role in {"user", "assistant"} else "user"
        content_parts = _chat_content_to_responses_content(message.get("content"))
        if not content_parts:
            content_parts = [{"type": "input_text", "text": ""}]
        content_field = "input_text" if resp_role == "user" else "output_text"
        # For user messages use input_text parts; for assistant use output_text.
        adjusted = []
        for part in content_parts:
            if part.get("type") == "input_text" and resp_role == "assistant":
                adjusted.append({"type": "output_text", "text": part.get("text", ""), "annotations": []})
            elif part.get("type") == "output_text" and resp_role == "user":
                adjusted.append({"type": "input_text", "text": part.get("text", "")})
            else:
                adjusted.append(part)
        input_items.append({
            "type": "message",
            "role": resp_role,
            "content": adjusted or [{"type": "input_text", "text": ""}],
        })

    instructions = "\n\n".join(instructions_parts) if instructions_parts else None
    return instructions, input_items


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


def _chat_tools_to_responses_tools(value: Any) -> list[dict[str, Any]]:
    """Convert chat-completions ``tools`` into Responses ``tools``."""
    if not isinstance(value, list):
        return []
    tools: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        tool: dict[str, Any] = {"type": "function", "name": name}
        description = function.get("description")
        if isinstance(description, str):
            tool["description"] = description
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            tool["parameters"] = parameters
        strict = function.get("strict")
        if isinstance(strict, bool):
            tool["strict"] = strict
        tools.append(tool)
    return tools


def _chat_tool_choice_to_responses_tool_choice(value: Any) -> Any:
    """Convert chat-completions ``tool_choice`` into Responses ``tool_choice``."""
    if isinstance(value, str):
        if value == "auto":
            return "auto"
        if value == "none":
            return "none"
        if value == "required":
            return "required"
        return value
    if isinstance(value, dict) and value.get("type") == "function":
        function = value.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return {"type": "function", "name": function["name"]}
    return value


def _chat_completions_request_to_responses_body(body: bytes) -> bytes:
    """Convert an inbound Chat Completions request into a Responses API request body."""
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return body

    instructions, input_items = _chat_messages_to_responses_input(payload.get("messages"))
    if not input_items:
        input_items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": ""}]}]

    responses_payload: dict[str, Any] = {
        "model": payload.get("model"),
        "input": input_items,
    }
    if isinstance(instructions, str) and instructions.strip():
        responses_payload["instructions"] = instructions

    for key in ("stream", "temperature", "top_p", "presence_penalty", "frequency_penalty", "parallel_tool_calls"):
        if key in payload:
            responses_payload[key] = payload[key]
    if "max_tokens" in payload:
        responses_payload["max_output_tokens"] = payload["max_tokens"]
    if "max_output_tokens" in payload:
        responses_payload["max_output_tokens"] = payload["max_output_tokens"]

    tools = _chat_tools_to_responses_tools(payload.get("tools"))
    if tools:
        responses_payload["tools"] = tools
    tool_choice = _chat_tool_choice_to_responses_tool_choice(payload.get("tool_choice"))
    if tool_choice is not None:
        responses_payload["tool_choice"] = tool_choice

    return json.dumps(responses_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


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
    """Convert a Responses API response body into a Chat Completions response body."""
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return body
    output = payload.get("output")
    has_error_signal = (
        payload.get("error") is not None
        or isinstance(payload.get("detail"), str)
        or payload.get("status") in {"failed", "incomplete"}
    )
    if has_error_signal and (not isinstance(output, list) or not output):
        return _chat_completion_error_body(payload)

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                            text = part.get("text")
                            if isinstance(text, str):
                                text_parts.append(text)
            elif item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                name = _chat_function_name_from_response_item(item)
                arguments = item.get("arguments")
                if isinstance(name, str) and name:
                    tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments if isinstance(arguments, str) else "",
                        },
                    })

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None

    choice: dict[str, Any] = {
        "index": 0,
        "message": message,
        "finish_reason": "tool_calls" if tool_calls else "stop",
    }

    chat_payload: dict[str, Any] = {
        "id": payload.get("id") if isinstance(payload.get("id"), str) else f"chatcmpl_{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model"),
        "choices": [choice],
    }
    usage = payload.get("usage")
    if isinstance(usage, dict):
        chat_payload["usage"] = usage

    return json.dumps(chat_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _chat_completion_body_to_stream_chunks(body: bytes) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return []

    response_id = payload.get("id") if isinstance(payload.get("id"), str) else f"chatcmpl_{uuid.uuid4().hex[:12]}"
    model = payload.get("model")
    chunks: list[dict[str, Any]] = [
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    choices = payload.get("choices")
    if isinstance(choices, list):
        for fallback_index, choice in enumerate(choices):
            if not isinstance(choice, Mapping):
                continue
            index = choice.get("index")
            index = index if isinstance(index, int) else fallback_index
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                chunks.append(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": index, "delta": {"content": content}, "finish_reason": None}],
                    }
                )
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for fallback_tool_index, tool_call in enumerate(tool_calls):
                    if not isinstance(tool_call, Mapping):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    name = function.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    tool_index = tool_call.get("index")
                    tool_index = tool_index if isinstance(tool_index, int) else fallback_tool_index
                    call_id = tool_call.get("id") if isinstance(tool_call.get("id"), str) else f"call_{uuid.uuid4().hex[:12]}"
                    arguments = function.get("arguments") if isinstance(function.get("arguments"), str) else ""
                    chunks.append(
                        {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": index,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": tool_index,
                                                "id": call_id,
                                                "type": "function",
                                                "function": {"name": name, "arguments": arguments},
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
            finish_reason = choice.get("finish_reason")
            if not isinstance(finish_reason, str):
                finish_reason = "tool_calls" if isinstance(tool_calls, list) and tool_calls else "stop"
            chunks.append(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": index, "delta": {}, "finish_reason": finish_reason}],
                }
            )
    return chunks


def _chat_completion_error_body(payload: Mapping[str, Any]) -> bytes:
    error = payload.get("error")
    if isinstance(error, Mapping):
        normalized_error = dict(error)
        if not isinstance(normalized_error.get("message"), str):
            normalized_error["message"] = json.dumps(error, ensure_ascii=True, separators=(",", ":"))
        normalized_error.setdefault("type", "upstream_error")
        normalized_error.setdefault("code", payload.get("code"))
    else:
        error_type = payload.get("type") if isinstance(payload.get("type"), str) else "upstream_error"
        detail = payload.get("detail")
        message = error if isinstance(error, str) and error else detail or "Upstream request failed"
        if error_type == "upstream_stream_error" and isinstance(detail, str) and detail:
            message = detail
        normalized_error = {
            "message": message,
            "type": error_type,
            "code": payload.get("code") or (error if error_type == "upstream_stream_error" else None),
        }
    if isinstance(payload.get("status"), int):
        normalized_error.setdefault("status", payload.get("status"))
    if isinstance(payload.get("upstream"), str):
        normalized_error.setdefault("upstream", payload.get("upstream"))
    return json.dumps({"error": normalized_error}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


class UpstreamStreamInterruptedError(RuntimeError):
    """Raised when an upstream stream is interrupted before downstream output starts."""

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(str(cause))


class UpstreamStreamIncompleteError(RuntimeError):
    """Raised when an upstream stream ends without a terminal event."""


class UpstreamEmptyCompletedResponseError(UpstreamStreamIncompleteError):
    """Raised when a third-party Responses stream completes with no visible output."""


class UpstreamStreamErrorEvent(RuntimeError):
    """Raised when an upstream Responses SSE stream emits an error event."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        super().__init__(_stream_error_event_detail(payload))


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


def _responses_events_have_completed(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        if isinstance(event, Mapping) and event.get("type") == "response.completed":
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
    """Convert Responses API SSE events into Chat Completions stream chunks.

    Mirrors :func:`_chat_stream_chunks_to_response_events`.  Text deltas become
    ``delta.content`` fragments; function_call argument deltas become
    ``delta.tool_calls`` fragments.  A final chunk with ``finish_reason`` is
    emitted when the response completes.
    """
    if require_completed and not _responses_events_have_completed(events):
        raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")

    chunks: list[dict[str, Any]] = []
    tool_states: dict[str, dict[str, Any]] = {}
    model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None

    def tool_state(item_id: str) -> dict[str, Any]:
        if item_id not in tool_states:
            index = len(tool_states)
            tool_states[item_id] = {
                "index": index,
                "id": "",
                "name": "",
                "arguments": "",
                "emitted_header": False,
            }
        return tool_states[item_id]

    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if event_type == "response.created":
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                response_id = response_obj.get("id") or response_id
                model = response_obj.get("model") or model
            # Emit an initial role chunk — many OpenAI-compatible clients
            # (including ZCode) expect the first delta to carry {"role":"assistant"}.
            chunks.append({
                "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            })
            continue
        if event_type == "response.output_text.delta":
            delta_text = event.get("delta")
            if isinstance(delta_text, str) and delta_text:
                chunks.append({
                    "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}],
                })
            continue
        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "function_call":
                item_id = item.get("id") or item.get("call_id") or ""
                state = tool_state(item_id)
                state["id"] = item.get("call_id") or state["id"]
                state["name"] = _chat_function_name_from_response_item(item) or state["name"]
                if state["id"] and state["name"] and not state["emitted_header"]:
                    chunks.append({
                        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": state["index"],
                                    "id": state["id"],
                                    "type": "function",
                                    "function": {"name": state["name"], "arguments": ""},
                                }],
                            },
                            "finish_reason": None,
                        }],
                    })
                    state["emitted_header"] = True
            continue
        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id") or ""
            state = tool_state(item_id)
            delta_args = event.get("delta")
            if isinstance(delta_args, str) and delta_args:
                if not state["emitted_header"]:
                    # Header not seen yet; emit it now with the delta.
                    chunks.append({
                        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": state["index"],
                                    "id": state["id"] or f"call_{uuid.uuid4().hex[:12]}",
                                    "type": "function",
                                    "function": {"name": state["name"], "arguments": delta_args},
                                }],
                            },
                            "finish_reason": None,
                        }],
                    })
                    state["emitted_header"] = True
                else:
                    chunks.append({
                        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": state["index"],
                                    "function": {"arguments": delta_args},
                                }],
                            },
                            "finish_reason": None,
                        }],
                    })
            continue
        if event_type == "response.completed":
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                output = response_obj.get("output")
                if isinstance(output, list):
                    has_tool = any(
                        isinstance(i, Mapping) and i.get("type") == "function_call"
                        for i in output
                    )
                    finish_reason = "tool_calls" if has_tool else "stop"
                else:
                    finish_reason = "stop"
            else:
                finish_reason = "stop"
            continue

    if finish_reason is None:
        finish_reason = "stop"
    chunks.append({
        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    })
    return chunks


class _ResponsesToChatStreamConverter:
    def __init__(self) -> None:
        self.tool_states: dict[str, dict[str, Any]] = {}
        self.model: str | None = None
        self.response_id: str | None = None
        self.completed = False

    def _tool_state(self, item_id: str) -> dict[str, Any]:
        if item_id not in self.tool_states:
            index = len(self.tool_states)
            self.tool_states[item_id] = {
                "index": index,
                "id": "",
                "name": "",
                "arguments": "",
                "emitted_header": False,
            }
        return self.tool_states[item_id]

    def _chunk(self, delta: Mapping[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return {
            "id": self.response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.model,
            "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish_reason}],
        }

    def chunks_for_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        event_type = event.get("type")
        if event_type == "response.created":
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                self.response_id = response_obj.get("id") or self.response_id
                self.model = response_obj.get("model") or self.model
            return [self._chunk({"role": "assistant"})]
        if event_type == "response.output_text.delta":
            delta_text = event.get("delta")
            if isinstance(delta_text, str) and delta_text:
                return [self._chunk({"content": delta_text})]
            return []
        if event_type == "response.output_item.added":
            item = event.get("item")
            if not (isinstance(item, Mapping) and item.get("type") == "function_call"):
                return []
            item_id = item.get("id") or item.get("call_id") or ""
            state = self._tool_state(str(item_id))
            state["id"] = item.get("call_id") or state["id"]
            state["name"] = item.get("name") or state["name"]
            if not (state["id"] and state["name"] and not state["emitted_header"]):
                return []
            state["emitted_header"] = True
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "id": state["id"],
                                "type": "function",
                                "function": {"name": state["name"], "arguments": ""},
                            }
                        ]
                    }
                )
            ]
        if event_type == "response.function_call_arguments.delta":
            item_id = str(event.get("item_id") or "")
            state = self._tool_state(item_id)
            delta_args = event.get("delta")
            if not (isinstance(delta_args, str) and delta_args):
                return []
            if not state["emitted_header"]:
                state["emitted_header"] = True
                call_id = state["id"] or f"call_{uuid.uuid4().hex[:12]}"
                return [
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": state["index"],
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": state["name"], "arguments": delta_args},
                                }
                            ]
                        }
                    )
                ]
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "function": {"arguments": delta_args},
                            }
                        ]
                    }
                )
            ]
        if event_type == "response.completed":
            self.completed = True
            finish_reason = "stop"
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                output = response_obj.get("output")
                if isinstance(output, list) and any(
                    isinstance(item, Mapping) and item.get("type") == "function_call"
                    for item in output
                ):
                    finish_reason = "tool_calls"
            return [self._chunk({}, finish_reason=finish_reason)]
        return []


class _ChatToResponsesStreamConverter:
    def __init__(self) -> None:
        self.response_id = f"resp_{uuid.uuid4().hex[:12]}"
        self.model: str | None = None
        self.item_id = f"msg_{uuid.uuid4().hex[:12]}"
        self.text_parts: list[str] = []
        self.message_output_index: int | None = None
        self.next_output_index = 0
        self.tool_states: dict[int, dict[str, Any]] = {}
        self.created = False
        self.message_started = False
        self.completed = False

    def _allocate_output_index(self) -> int:
        output_index = self.next_output_index
        self.next_output_index += 1
        return output_index

    def _created_events(self) -> list[dict[str, Any]]:
        if self.created:
            return []
        self.created = True
        response = {
            "id": self.response_id,
            "object": "response",
            "status": "in_progress",
            "model": self.model,
            "output": [],
        }
        return [
            {"type": "response.created", "response": response},
            {"type": "response.in_progress", "response": response},
        ]

    def _message_start_events(self) -> list[dict[str, Any]]:
        events = self._created_events()
        if self.message_started:
            return events
        if self.message_output_index is None:
            self.message_output_index = self._allocate_output_index()
        self.message_started = True
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": self.message_output_index,
                    "item": {
                        "id": self.item_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
                {
                    "type": "response.content_part.added",
                    "output_index": self.message_output_index,
                    "item_id": self.item_id,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ]
        )
        return events

    def _tool_state(self, index: int) -> dict[str, Any]:
        if index not in self.tool_states:
            self.tool_states[index] = {
                "output_index": self._allocate_output_index(),
                "item_id": "",
                "call_id": "",
                "name": "",
                "arguments": [],
                "added": False,
                "done": False,
            }
        return self.tool_states[index]

    def _tool_added_events(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        if state["added"] or not state["call_id"] or not state["name"]:
            return []
        events = self._created_events()
        state["item_id"] = f"fc_{state['call_id']}"
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": state["output_index"],
                "item": {
                    "id": state["item_id"],
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": state["call_id"],
                    "name": state["name"],
                    "arguments": "",
                },
            }
        )
        state["added"] = True
        return events

    def _complete_events(self) -> list[dict[str, Any]]:
        if self.completed:
            return []
        self.completed = True
        events = self._created_events()
        output_by_index: dict[int, dict[str, Any]] = {}
        for state in sorted(self.tool_states.values(), key=lambda item: item["output_index"]):
            events.extend(self._tool_added_events(state))
            if not state["added"] or state["done"]:
                continue
            arguments = "".join(state["arguments"])
            item = {
                "id": state["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": arguments,
            }
            events.extend(
                [
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "arguments": arguments,
                    },
                    {"type": "response.output_item.done", "output_index": state["output_index"], "item": item},
                ]
            )
            state["done"] = True
            output_by_index[state["output_index"]] = item
        if self.message_started:
            text = "".join(self.text_parts)
            output_index = self.message_output_index if self.message_output_index is not None else 0
            item = {
                "id": self.item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            events.extend(
                [
                    {
                        "type": "response.output_text.done",
                        "item_id": self.item_id,
                        "output_index": output_index,
                        "content_index": 0,
                        "text": text,
                    },
                    {"type": "response.output_item.done", "output_index": output_index, "item": item},
                ]
            )
            output_by_index[output_index] = item
        output = [
            item
            for _, item in sorted(output_by_index.items(), key=lambda pair: pair[0])
        ]
        events.append(
            {
                "type": "response.completed",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "status": "completed",
                    "model": self.model,
                    "output": output,
                },
            }
        )
        return events

    def events_for_done(self) -> list[dict[str, Any]]:
        return self._complete_events()

    def events_for_chunk(self, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
        if isinstance(chunk.get("model"), str):
            self.model = chunk.get("model")
        events: list[dict[str, Any]] = []
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return events
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                content = delta.get("content")
                if isinstance(content, str) and content:
                    self.text_parts.append(content)
                    events.extend(self._message_start_events())
                    events.append(
                        {
                            "type": "response.output_text.delta",
                            "item_id": self.item_id,
                            "output_index": self.message_output_index if self.message_output_index is not None else 0,
                            "content_index": 0,
                            "delta": content,
                        }
                    )
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for fallback_index, tool_call in enumerate(tool_calls):
                        if not isinstance(tool_call, Mapping):
                            continue
                        raw_index = tool_call.get("index", fallback_index)
                        index = raw_index if isinstance(raw_index, int) else fallback_index
                        state = self._tool_state(index)
                        call_id = tool_call.get("id")
                        if isinstance(call_id, str) and call_id and not state["call_id"]:
                            state["call_id"] = call_id
                        function = tool_call.get("function")
                        argument_delta: str | None = None
                        if isinstance(function, Mapping):
                            name = function.get("name")
                            if isinstance(name, str) and name and not state["name"]:
                                state["name"] = name
                            arguments = function.get("arguments")
                            if isinstance(arguments, str) and arguments:
                                state["arguments"].append(arguments)
                                argument_delta = arguments
                        events.extend(self._tool_added_events(state))
                        if state["added"] and argument_delta:
                            events.append(
                                {
                                    "type": "response.function_call_arguments.delta",
                                    "item_id": state["item_id"],
                                    "output_index": state["output_index"],
                                    "delta": argument_delta,
                                }
                            )
            if choice.get("finish_reason") is not None:
                events.extend(self._complete_events())
        return events


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
    """Reconstruct a non-streaming Responses API body from SSE events.

    Used when the upstream forces streaming (e.g. chatgpt.com) but the caller
    requested a non-streaming response.  Collects output items and text from
    the event stream into a single ``response`` object.
    """
    if require_completed and not _responses_events_have_completed(events):
        raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")

    output: list[dict[str, Any]] = []
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    model: str | None = None
    text_parts: list[str] = []
    current_item: dict[str, Any] | None = None
    usage: Mapping[str, Any] | None = None
    response_payload: dict[str, Any] = {}

    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if event_type == "response.created":
            resp = event.get("response")
            if isinstance(resp, Mapping):
                response_payload.update(dict(resp))
                response_id = resp.get("id") or response_id
                model = resp.get("model") or model
        elif event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict):
                current_item = dict(item)
        elif event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                output.append(dict(item))
                current_item = None
        elif event_type == "response.function_call_arguments.done":
            # Ensure the function_call item is in output with final arguments.
            args = event.get("arguments")
            if current_item and isinstance(args, str):
                current_item["arguments"] = args
        elif event_type == "response.completed":
            resp = event.get("response")
            if isinstance(resp, Mapping):
                response_payload.update(dict(resp))
                response_id = resp.get("id") or response_id
                model = resp.get("model") or model
                usage = _usage_from_payload(resp) or usage
                resp_output = resp.get("output")
                if isinstance(resp_output, list) and not output:
                    output = [dict(i) for i in resp_output if isinstance(i, dict)]

    # If we collected text deltas but no output_item.done for the message,
    # synthesize a message item.
    if text_parts and not any(i.get("type") == "message" for i in output):
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(text_parts), "annotations": []}],
        })

    payload: dict[str, Any] = dict(response_payload)
    payload["id"] = response_id
    payload.setdefault("object", "response")
    payload.setdefault("status", "completed")
    if model is not None or "model" not in payload:
        payload["model"] = model
    if output or not isinstance(payload.get("output"), list):
        payload["output"] = output
    if usage is not None:
        payload["usage"] = dict(usage)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _response_body_to_response_sse_events(body: bytes) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("error"), (str, Mapping)):
        return []

    response = dict(payload)
    response_id = response.get("id") if isinstance(response.get("id"), str) else f"resp_{uuid.uuid4().hex[:12]}"
    response["id"] = response_id
    response.setdefault("object", "response")
    response.setdefault("status", "completed")
    output = response.get("output")
    output_items = output if isinstance(output, list) else []
    model_value = response.get("model")

    created_response = dict(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": created_response},
        {"type": "response.in_progress", "response": created_response},
    ]

    for output_index, raw_item in enumerate(output_items):
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item_type = item.get("type")
        item_id = item.get("id") if isinstance(item.get("id"), str) else f"item_{output_index}"
        item["id"] = item_id
        if item_type == "message":
            in_progress_item = dict(item)
            in_progress_item["status"] = "in_progress"
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            text = "".join(_collect_text_fragments(item.get("content")))
            if text:
                part = {"type": "output_text", "text": "", "annotations": []}
                events.append(
                    {
                        "type": "response.content_part.added",
                        "output_index": output_index,
                        "item_id": item_id,
                        "content_index": 0,
                        "part": part,
                    }
                )
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "output_index": output_index,
                        "item_id": item_id,
                        "content_index": 0,
                        "delta": text,
                    }
                )
                events.append(
                    {
                        "type": "response.output_text.done",
                        "output_index": output_index,
                        "item_id": item_id,
                        "content_index": 0,
                        "text": text,
                    }
                )
                events.append(
                    {
                        "type": "response.content_part.done",
                        "output_index": output_index,
                        "item_id": item_id,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": text, "annotations": []},
                    }
                )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                }
            )
            continue
        if item_type == "function_call":
            in_progress_item = dict(item)
            in_progress_item["status"] = "in_progress"
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            arguments = item.get("arguments")
            if isinstance(arguments, str) and arguments:
                events.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": output_index,
                        "item_id": item_id,
                        "delta": arguments,
                    }
                )
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": output_index,
                    "item_id": item_id,
                    "arguments": arguments if isinstance(arguments, str) else "",
                }
            )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                }
            )

    response["status"] = "completed"
    if model_value is not None:
        response["model"] = model_value
    events.append({"type": "response.completed", "response": response})
    return events


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


def _structured_tool_function_call_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if item.get("type") != "function_call":
        return None
    tool_name = _multi_agent_function_call_name(item)
    if tool_name is not None:
        rewritten = dict(item)
        rewritten.pop("namespace", None)
        rewritten["name"] = f"multi_agent_v1__{tool_name}"
        normalized, _, args_changed = _normalize_multi_agent_arguments(rewritten.get("arguments"), tool_name)
        if args_changed:
            rewritten["arguments"] = normalized
        return rewritten
    node_name = _node_repl_function_call_name(item)
    if node_name is not None:
        rewritten = dict(item)
        rewritten.pop("namespace", None)
        rewritten["name"] = f"{NODE_REPL_NAMESPACE}__{node_name}"
        return rewritten
    return dict(item)


def _rewrite_structured_tool_input_items(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    rewritten_items: list[Any] = []
    preserved_structured_call_ids: set[str] = set()
    available_function_names = _function_tool_names(payload.get("tools"))
    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue
        if item.get("type") == "function_call":
            function_name = item.get("name")
            preserve_available_function = (
                isinstance(function_name, str) and function_name in available_function_names
            )
            if (
                preserve_available_function
                or _multi_agent_function_call_name(item) is not None
                or _node_repl_function_call_name(item) is not None
            ):
                call_id = item.get("call_id")
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
    open_agent_ids: list[str] | None = None,
    wait_agent_ids: list[str] | None = None,
    close_agent_ids: list[str] | None = None,
) -> bool:
    tools = payload.get("tools")
    if tools is None:
        tools = []
        payload["tools"] = tools
    if not isinstance(tools, list):
        return False

    changed = False
    flattened_namespace_tools = _flatten_namespace_function_tools(tools)
    if strip_namespace_tools:
        filtered_tools = [tool for tool in tools if not _is_flattened_namespace_schema(tool)]
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
    additions = []
    if include_tool_search:
        additions.append(TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL)
    if include_multi_agent_tools:
        additions.extend(
            _multi_agent_explicit_function_tools(
                include_spawn_agent=include_spawn_agent,
                include_wait_agent=include_wait_agent,
                include_close_agent=include_close_agent,
                include_resume_agent=include_resume_agent,
                include_send_input=include_send_input,
                open_agent_ids=open_agent_ids,
                wait_agent_ids=wait_agent_ids,
                close_agent_ids=close_agent_ids,
            )
        )
    additions.extend(flattened_namespace_tools)
    if not include_multi_agent_tools:
        additions = [tool for tool in additions if not _is_multi_agent_tool_schema(tool)]
    if not include_node_repl_tools:
        additions = [tool for tool in additions if not _is_node_repl_tool_schema(tool)]
    if excluded_tool_names:
        additions = [
            tool
            for tool in additions
            if not (
                isinstance(tool, Mapping)
                and tool.get("type") == "function"
                and tool.get("name") in excluded_tool_names
            )
        ]

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
        changed = True
    return changed


def _filter_tools_for_subagent_coordinator(payload: dict[str, Any], *, include_node_repl_tools: bool) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [
        tool
        for tool in tools
        if _is_multi_agent_tool_schema(tool)
        or (include_node_repl_tools and _is_node_repl_tool_schema(tool))
    ]
    if len(filtered_tools) == len(tools):
        return False
    payload["tools"] = filtered_tools
    return True


def _filter_tools_for_subagent_worker(payload: dict[str, Any]) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [
        tool
        for tool in tools
        if not _is_multi_agent_tool_schema(tool)
        and not _is_mcp_or_codex_app_tool_schema(tool)
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


def _normalize_third_party_tool_call(value: Any) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _normalize_third_party_tool_call(item)
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    changed = False
    rewritten = dict(value)
    if value.get("type") == "function_call" and value.get("name") == "tool_search":
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
        if argument_key is not None and _json_argument_string_needs_repair(value.get(argument_key)):
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
        replacement, item_changed = _normalize_third_party_tool_call(item)
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
    for item in input_items:
        item_type = item.get("type") if isinstance(item, dict) else None
        if isinstance(item_type, str) and item_type in INTERNAL_INPUT_ITEM_TYPES:
            call_id = item.get("call_id")
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
    if _is_raw_provider_probe_context(context):
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
    if bool(context.get("subagent_worker_context")) or _is_raw_provider_probe_context(context):
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
    if not bool((event_context or {}).get("subagent_worker_context")):
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


def _required_subagent_call_item_like(spec: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
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


def _coerce_required_subagent_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
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
    else:
        coerced_item_ids = set()
    rewritten, changed = _coerce_required_subagent_tool_calls_inner(value, spec, coerced_item_ids)
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
        if not isinstance(expected, str) or value.get("arguments") == expected:
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
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _coerce_required_subagent_tool_calls_inner(item, spec, coerced_item_ids)
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if value.get("type") == "response.function_call_arguments.done":
        item_id = value.get("item_id")
        if not isinstance(item_id, str) or item_id not in coerced_item_ids:
            return value, False
        arguments = spec.get("arguments") if isinstance(spec.get("arguments"), Mapping) else {}
        expected = json.dumps(dict(arguments), ensure_ascii=True, separators=(",", ":"))
        if value.get("arguments") != expected:
            rewritten = dict(value)
            rewritten["arguments"] = expected
            return rewritten, True
        return value, False

    if _multi_agent_function_call_name(value) is not None:
        replacement = _required_subagent_call_item_like(spec, value)
        item_id = replacement.get("id")
        if isinstance(item_id, str) and item_id:
            coerced_item_ids.add(item_id)
        return (replacement, True) if replacement != value else (value, False)

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _coerce_required_subagent_tool_calls_inner(item, spec, coerced_item_ids)
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
        upstream_model = upstream.get("upstream_model")
        if isinstance(model_id, str) and isinstance(upstream_model, str) and upstream_model and model_id != upstream_model:
            return _replace_embedded_model(body, model_id, upstream_model)
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
            if upstream_is_third_party and _rewrite_internal_input_items(next_payload):
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
    if upstream_is_third_party and _rewrite_internal_input_items(next_payload):
        changed = True
    if not changed:
        return body
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _is_raw_provider_probe_context(event_context: Mapping[str, Any] | None) -> bool:
    return bool((event_context or {}).get("raw_provider_probe"))


def compatible_request_body(
    body: bytes,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
    event_context: Mapping[str, Any] | None = None,
    inject_codex_tools: bool = True,
    behavior_profile: str = BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        upstream_model = upstream.get("upstream_model")
        if isinstance(model_id, str) and isinstance(upstream_model, str) and upstream_model and model_id != upstream_model:
            return _replace_embedded_model(body, model_id, upstream_model)
        return body

    if not isinstance(payload, dict):
        return body

    upstream_name = upstream.get("name")
    upstream_model = upstream.get("upstream_model")
    requested_model = payload.get("model")
    changed = False
    if behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH:
        return official_passthrough_request_body(body, payload, upstream, model_id=model_id)

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

    raw_provider_probe = _is_raw_provider_probe_context(event_context)
    tool_protocol = _external_tool_protocol(upstream)
    guidance_enabled = subagent_guidance_enabled(event_context)
    semantic_repair_enabled = subagent_semantic_repair_enabled(event_context)
    if isinstance(event_context, dict):
        event_context["tool_protocol"] = tool_protocol
    if raw_provider_probe:
        pass
    else:
        if tool_protocol in STRUCTURED_TOOL_PROTOCOLS:
            if _rewrite_structured_tool_input_items(payload, event_context=event_context, upstream_name=upstream_name):
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
    input_items = payload.get("input")
    include_tool_search = False
    subagent_worker_context = (
        not raw_provider_probe
        and tool_protocol in {"text_compat", "chat_tools", "responses_structured"}
        and is_worker_subagent_request(input_items)
    )
    subagent_state = (
        build_subagent_state(input_items)
        if (
            not raw_provider_probe
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
        not raw_provider_probe and _has_completed_single_step_node_repl_context(input_items)
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
    allow_codex_tools = tool_protocol != "none"
    if inject_codex_tools and allow_codex_tools and not raw_provider_probe:
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
            include_node_repl_for_subagent_workflow = (
                restrict_to_subagent_coordinator_tools
                and not node_repl_single_step_complete
                and not subagent_workflow_plan_read_complete
                and not bool(getattr(subagent_state, "dynamic_dag_intent", False))
                and not bool(subagent_state.agents if subagent_state is not None else {})
            )
            if subagent_worker_context and _filter_tools_for_subagent_worker(payload):
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
            if _inject_explicit_codex_tools(
                payload,
                include_tool_search=include_tool_search,
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
                open_agent_ids=open_agent_ids,
                wait_agent_ids=wait_agent_ids,
                close_agent_ids=close_agent_ids,
            ):
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
                if (
                    subagent_workflow_plan_read_required
                    and include_node_repl_for_subagent_workflow
                    and "mcp__node_repl__js" in _function_tool_names(payload.get("tools"))
                ):
                    required_tool_choice_name = "mcp__node_repl__js"
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
                _write_adapter_event(
                    event_context,
                    "required_tool_tools_restricted",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                    tool_name=required_tool_choice_name,
                )
                changed = True
            if semantic_repair_enabled and _set_required_subagent_tool_choice(
                payload,
                required_tool_choice_name,
                event_context=event_context,
                upstream=upstream_name,
            ):
                changed = True
    model_id = payload.get("model")
    max_output_tokens = catalog_max_output_tokens(model_id) if isinstance(model_id, str) else None
    if max_output_tokens is not None:
        requested_max_output_tokens = payload.get("max_output_tokens")
        if not isinstance(requested_max_output_tokens, int) or requested_max_output_tokens > max_output_tokens:
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
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, dict):
            effort = reasoning.get("effort")
            replacement = OLLAMA_REASONING_EFFORT_ALIASES.get(effort) if isinstance(effort, str) else None
            if replacement is not None:
                reasoning["effort"] = replacement
                changed = True
        else:
            replacement = OLLAMA_REASONING_EFFORT_ALIASES.get(reasoning) if isinstance(reasoning, str) else None
            if replacement is not None:
                payload["reasoning"] = replacement
                changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


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

    changed = _hide_reasoning_text(payload)
    payload, alias_changed = _normalize_third_party_tool_call(payload)
    if alias_changed:
        _write_adapter_event(
            event_context,
            "third_party_tool_call_alias_normalized",
            upstream=upstream_name,
            surface="body",
        )
    changed = changed or alias_changed
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
    payload, required_tool_changed = _coerce_required_subagent_tool_calls(payload, event_context)
    changed = changed or required_tool_changed
    payload, required_call_changed = _repair_missing_required_subagent_call_payload(payload, event_context)
    changed = changed or required_call_changed
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

    if _is_reasoning_text_stream_event(payload):
        return b""

    changed = _hide_reasoning_text(payload)
    payload, alias_changed = _normalize_third_party_tool_call(payload)
    if alias_changed:
        _write_adapter_event(
            event_context,
            "third_party_tool_call_alias_normalized",
            upstream=upstream_name,
            surface="sse",
        )
    changed = changed or alias_changed
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
    payload, required_tool_changed = _coerce_required_subagent_tool_calls(payload, event_context)
    changed = changed or required_tool_changed
    repaired_line = _repair_missing_required_subagent_call_sse_line(payload, event_context, line_ending)
    if repaired_line is not None:
        return repaired_line
    if not changed:
        return line
    return _sse_json_line(payload, line_ending)


def safe_upstream_error_detail(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    source = reason if reason is not None else exc
    detail = f"{type(source).__name__}: {source}"
    detail = detail.replace("\r", " ").replace("\n", " ")
    if "Bearer " in detail:
        detail = detail.split("Bearer ", 1)[0] + "Bearer [redacted]"
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
class RouteDecision:
    behavior_profile: str
    selected_upstream_format: str
    wire_format_adapter: str
    codex_semantic_adapter: str
    request_kind_policy: str
    retry_policy: str
    usage_policy: str
    repair_policy: str


def behavior_profile_for_request(
    upstream: Mapping[str, Any],
    request_context: Mapping[str, str],
    *,
    inbound_format: str,
) -> str:
    if str(upstream.get("name")) != "official":
        return BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY
    if (
        gateway_official_http_passthrough_enabled()
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


def route_decision_for_request(
    upstream: Mapping[str, Any],
    request_context: Mapping[str, str],
    *,
    inbound_format: str,
    provider_hint: str | None = None,
) -> RouteDecision:
    upstream_name = str(upstream.get("name") or "")
    upstream_format = str(upstream.get("upstream_format") or "responses")
    if upstream_format == "auto":
        upstream_format = "responses"
    wire_adapter = _wire_format_adapter(inbound_format, upstream_format)

    if upstream_name != "official" and _is_codex_app_context(request_context):
        return RouteDecision(
            behavior_profile=BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
            selected_upstream_format=upstream_format,
            wire_format_adapter=wire_adapter,
            codex_semantic_adapter=CODEX_SEMANTIC_EXTERNAL_ADAPTER,
            request_kind_policy=REQUEST_KIND_GATEWAY,
            retry_policy=RETRY_GATEWAY_FULL,
            usage_policy=USAGE_SYNC_CAPTURE,
            repair_policy=REPAIR_CODEX_SUBAGENT,
        )

    if upstream_name == "official" and request_context.get("client_id") == "unknown":
        return RouteDecision(
            behavior_profile=behavior_profile_for_request(
                upstream,
                request_context,
                inbound_format=inbound_format,
            ),
            selected_upstream_format=upstream_format,
            wire_format_adapter=wire_adapter,
            codex_semantic_adapter=CODEX_SEMANTIC_NONE,
            request_kind_policy=REQUEST_KIND_GATEWAY,
            retry_policy=RETRY_GATEWAY_FULL,
            usage_policy=USAGE_SYNC_CAPTURE,
            repair_policy=REPAIR_NONE,
        )

    if (
        upstream_name != "official"
        and provider_hint is None
        and not _is_codex_app_context(request_context)
        and not _has_explicit_third_party_client_identity(request_context)
    ):
        return RouteDecision(
            behavior_profile=BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
            selected_upstream_format=upstream_format,
            wire_format_adapter=wire_adapter,
            codex_semantic_adapter=CODEX_SEMANTIC_EXTERNAL_ADAPTER,
            request_kind_policy=REQUEST_KIND_GATEWAY,
            retry_policy=RETRY_GATEWAY_FULL,
            usage_policy=USAGE_SYNC_CAPTURE,
            repair_policy=REPAIR_NONE,
        )

    if not _is_codex_app_context(request_context):
        return RouteDecision(
            behavior_profile=BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
            selected_upstream_format=upstream_format,
            wire_format_adapter=wire_adapter,
            codex_semantic_adapter=CODEX_SEMANTIC_NONE,
            request_kind_policy=REQUEST_KIND_TRANSPARENT,
            retry_policy=RETRY_CONSERVATIVE_PRE_OUTPUT,
            usage_policy=USAGE_ASYNC_TAP,
            repair_policy=REPAIR_NONE,
        )

    behavior_profile = behavior_profile_for_request(
        upstream,
        request_context,
        inbound_format=inbound_format,
    )
    return RouteDecision(
        behavior_profile=behavior_profile,
        selected_upstream_format=upstream_format,
        wire_format_adapter=wire_adapter,
        codex_semantic_adapter=CODEX_SEMANTIC_NONE,
        request_kind_policy=REQUEST_KIND_GATEWAY,
        retry_policy=RETRY_GATEWAY_FULL,
        usage_policy=USAGE_SYNC_CAPTURE,
        repair_policy=REPAIR_NONE,
    )


def _route_decision_event_fields(decision: RouteDecision) -> dict[str, str]:
    return {
        "wire_format_adapter": decision.wire_format_adapter,
        "codex_semantic_adapter": decision.codex_semantic_adapter,
        "request_kind_policy": decision.request_kind_policy,
        "retry_policy": decision.retry_policy,
        "usage_policy": decision.usage_policy,
        "repair_policy": decision.repair_policy,
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


def vision_proxy_policy_for_route(decision: RouteDecision, behavior_profile: str | None = None) -> str:
    active_behavior_profile = behavior_profile or decision.behavior_profile
    if (
        active_behavior_profile == BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER
        and decision.codex_semantic_adapter == CODEX_SEMANTIC_EXTERNAL_ADAPTER
    ):
        return VISION_PROXY_CODEX_APP_ADAPTER
    if (
        active_behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
        and gateway_transparent_vision_proxy_enabled()
    ):
        return VISION_PROXY_TRANSPARENT_OVERLAY
    return VISION_PROXY_DISABLED


def _is_event_stream(headers: Mapping[str, str] | Any) -> bool:
    content_type = _get_header(headers, "Content-Type")
    if content_type and "text/event-stream" in content_type.lower():
        return True
    # Some upstreams (e.g. chatgpt.com/backend-api/codex) return SSE without
    # an explicit Content-Type header but do signal chunked transfer.
    transfer_encoding = _get_header(headers, "Transfer-Encoding")
    return bool(transfer_encoding and "chunked" in transfer_encoding.lower())


def _filtered_response_headers(
    headers: Mapping[str, str] | Any,
    is_event_stream: bool,
    content_length: int | None = None,
    content_type: str | None = None,
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
        outgoing.append((key, value))
    if content_type is not None:
        outgoing.append(("Content-Type", content_type))
    if content_length is not None:
        outgoing.append(("Content-Length", str(content_length)))
    return outgoing


def upstream_headers(
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
    drop_content_encoding: bool = False,
    behavior_profile: str | None = None,
    model_id: str | None = None,
) -> dict[str, str]:
    auth_mode = upstream.get("auth")
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
        incoming_auth = _get_header(incoming_headers, "Authorization")
        if incoming_auth:
            outgoing["Authorization"] = incoming_auth
    elif auth_mode == "ollama_api_key":
        api_key = os.environ.get("OLLAMA_API_KEY")
        if not api_key:
            raise ValueError("OLLAMA_API_KEY is not set")
        outgoing["Authorization"] = f"Bearer {api_key}"
    elif auth_mode == "api_key":
        api_key = upstream.get("api_key")
        if not api_key:
            raise ValueError(f"API key is not set for upstream: {upstream.get('name', 'unknown')}")
        outgoing["Authorization"] = f"Bearer {api_key}"
    elif auth_mode == "codex_auth":
        strict_official_passthrough = behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
        token = codex_access_token()
        outgoing["Authorization"] = f"Bearer {token}"
        # The chatgpt.com backend requires the account id header to identify
        # the subscription. Inject it from auth.json when not already present.
        if not _get_header(outgoing, "Chatgpt-account-id"):
            account = codex_account_id()
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
                session_id = str(uuid.uuid4())
                outgoing["Session-id"] = session_id
            if not _get_header(outgoing, "Thread-id"):
                outgoing["Thread-id"] = session_id
            if not _get_header(outgoing, "X-codex-window-id"):
                outgoing["X-codex-window-id"] = f"{session_id}:1"
            if not _get_header(outgoing, "X-client-request-id"):
                outgoing["X-client-request-id"] = str(uuid.uuid4())
    else:
        raise ValueError(f"unsupported upstream auth mode: {auth_mode}")

    return outgoing


def current_catalog_data(sync_first: bool = False) -> dict[str, Any]:
    if sync_first:
        try:
            sync_catalog()
        except Exception as exc:
            logger.warning("catalog sync failed before /v1/models: %s", type(exc).__name__)

    catalog_path = existing_generated_catalog_path()
    if not catalog_path.exists():
        return {"models": []}
    return catalog_with_vision_proxy_capabilities(
        catalog_with_openai_context_guard(
            catalog_with_official_fast_variants(
                json.loads(catalog_path.read_text(encoding="utf-8-sig"))
            )
        )
    )


def catalog_with_openai_context_guard(catalog: dict[str, Any]) -> dict[str, Any]:
    if not openai_context_guard_enabled():
        return catalog

    models = catalog.get("models")
    if not isinstance(models, list):
        return catalog

    def guarded_model(model: Any) -> Any:
        if not isinstance(model, Mapping):
            return model
        slug = canonical_model_id(str(model.get("slug", "")))
        if not slug.startswith("gpt-"):
            return model
        context_window = model.get("context_window")
        guarded_window = (
            min(context_window, OPENAI_CONTEXT_GUARD_CONTEXT_WINDOW)
            if isinstance(context_window, int) and context_window > 0
            else OPENAI_CONTEXT_GUARD_CONTEXT_WINDOW
        )
        return {
            **model,
            "context_window": guarded_window,
            "max_context_window": guarded_window,
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
        model = catalog.get(candidate)
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
        logger.warning("image proxy cache lookup failed: %s", type(exc).__name__)
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
        logger.warning("image proxy cache store failed: %s", type(exc).__name__)


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
        while True:
            line = response.readline()
            if not line:
                break
            event = _parse_sse_json_payload(line)
            if isinstance(event, Mapping):
                events.append(event)
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
        raise ImageProxyError("Vision model is not configured for Image Proxy")
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
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> bool:
    if not gateway_image_proxy_enabled():
        return False
    if target_model and model_supports_image(target_model, target_upstream):
        return False
    if not _value_contains_image(payload.get("input")):
        return False

    vision_model, vision_upstream = _image_proxy_vision_upstream()

    descriptions: dict[str, str] = {}
    progress_sent = False
    image_count = _image_proxy_unique_image_count(payload.get("input"), vision_model)

    def emit_progress_once() -> None:
        nonlocal progress_sent
        if progress_sent or progress_callback is None:
            return
        progress_callback(
            {
                "type": "image_proxy",
                "status": "reading",
                "image_count": image_count,
                "vision_model": canonical_model_id(vision_model),
            }
        )
        progress_sent = True

    def describe(part: Mapping[str, Any]) -> tuple[str, str]:
        cache_key = _image_proxy_cache_key(part, vision_model)
        if cache_key not in descriptions:
            if _image_proxy_cache_lookup(cache_key) is None:
                emit_progress_once()
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
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> bool:
    if not gateway_image_proxy_enabled():
        return False
    if target_model and model_supports_image(target_model, target_upstream):
        return False
    if not _value_contains_image(payload.get("messages")):
        return False

    vision_model, vision_upstream = _image_proxy_vision_upstream()
    descriptions: dict[str, str] = {}
    progress_sent = False
    image_count = _image_proxy_unique_image_count(payload.get("messages"), vision_model)

    def emit_progress_once() -> None:
        nonlocal progress_sent
        if progress_sent or progress_callback is None:
            return
        progress_callback(
            {
                "type": "image_proxy",
                "status": "reading",
                "image_count": image_count,
                "vision_model": canonical_model_id(vision_model),
            }
        )
        progress_sent = True

    def describe(part: Mapping[str, Any]) -> tuple[str, str]:
        cache_key = _image_proxy_cache_key(part, vision_model)
        if cache_key not in descriptions:
            if _image_proxy_cache_lookup(cache_key) is None:
                emit_progress_once()
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
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
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
        )
    return apply_image_proxy_to_responses_payload(
        payload,
        target_model,
        target_upstream,
        event_context=proxy_context,
        progress_callback=progress_callback,
    )


def enforce_text_only_image_boundary(
    payload: dict[str, Any],
    *,
    inbound_format: str,
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> bool:
    if target_model and model_supports_image(target_model, target_upstream):
        return False
    image_root = payload.get("messages") if inbound_format == "chat_completions" else payload.get("input")
    if not _value_contains_image(image_root):
        return False

    if gateway_image_proxy_enabled():
        changed = (
            apply_image_proxy_to_chat_payload(
                payload,
                target_model,
                target_upstream,
                event_context=event_context,
                progress_callback=progress_callback,
            )
            if inbound_format == "chat_completions"
            else apply_image_proxy_to_responses_payload(
                payload,
                target_model,
                target_upstream,
                event_context=event_context,
                progress_callback=progress_callback,
            )
        )
        if changed:
            _write_adapter_event(
                event_context,
                "image_proxy_boundary_guard_applied",
                target_model=canonical_model_id(target_model) if target_model else None,
                inbound_format=inbound_format,
            )
            return True
        image_root = payload.get("messages") if inbound_format == "chat_completions" else payload.get("input")
        if not _value_contains_image(image_root):
            return False

    model_label = canonical_model_id(target_model) if target_model else "the target model"
    raise ImageProxyError(
        f"{model_label} does not support image input and Image Proxy is disabled or could not replace the image."
    )


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


def _stream_retry_elapsed_limit_allows(started_at: float, delay_seconds: int) -> bool:
    limit_seconds = gateway_stream_retry_elapsed_limit_seconds()
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
) -> None:
    resolved_failure_class = failure_class or _upstream_failure_class(exc)
    _write_adapter_event(
        event_context,
        "upstream_retry",
        upstream=upstream_name,
        provider_id=upstream_name,
        upstream_format=upstream_format,
        request_kind=request_kind,
        retryable=True,
        failure_class=resolved_failure_class,
        status=_upstream_retry_status(exc),
        attempt=attempt,
        max_attempts=max_attempts,
        delay_ms=delay_seconds * 1000,
        error=type(exc).__name__,
        detail=safe_upstream_error_detail(exc),
        failure_phase=failure_phase or transport_failure_phase(exc),
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
        "detail": safe_upstream_error_detail(exc),
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


def _typed_error_code(
    *,
    error_type: str,
    error_code: str,
    exc: BaseException | None,
    status: int | None,
) -> str:
    if error_type == "gateway_auth_error":
        return "gateway.auth"
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
) -> dict[str, Any]:
    error_type = error or (type(exc).__name__ if exc is not None else "UpstreamStreamError")
    error_detail = detail or (safe_upstream_error_detail(exc) if exc is not None else "")
    failure_class = _upstream_failure_class(exc) if exc is not None else None
    if failure_class is None and error_type in {
        "upstream_stream_idle_timeout",
        "upstream_stream_incomplete",
        "UpstreamStreamError",
        "UpstreamProtocolError",
    }:
        failure_class = RETRY_FAILURE_QUICK_TRANSIENT
    payload = {
        "type": "upstream_stream_error",
        "status": status,
        "upstream": upstream_name,
        "error": error_type,
        "detail": error_detail,
        "retry_owner": "client",
    }
    if failure_class is not None:
        payload["failure_class"] = failure_class
        payload["retryable"] = failure_class != RETRY_FAILURE_PERMANENT
    payload["codexhub_error"] = _codexhub_error_payload(
        source=upstream_name,
        message=error_detail or error_type,
        status=status,
        exc=exc,
        error=error_type,
        error_type="upstream_stream_error",
        failure_class=failure_class,
    )
    return payload


def _downstream_sse_error_payload_for_inbound_format(error: DownstreamErrorSpec) -> dict[str, Any]:
    if error.inbound_format == "chat_completions":
        return _chat_completion_error_payload(
            upstream_name=error.upstream_name,
            status=error.status,
            exc=error.exc,
            error=error.error,
            detail=error.detail,
            error_type="upstream_stream_error",
        )
    if error.exc is not None:
        return _downstream_stream_error_payload(upstream_name=error.upstream_name, exc=error.exc)
    return _downstream_stream_error_payload(
        upstream_name=error.upstream_name,
        status=error.status,
        error=error.error or "UpstreamProtocolError",
        detail=error.detail or error.error or "upstream stream failed",
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
) -> dict[str, Any]:
    stream_error = _downstream_stream_error_payload(
        upstream_name=upstream_name,
        status=status,
        exc=exc,
        error=error,
        detail=detail,
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
) -> dict[str, Any]:
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    error_detail = detail or (safe_upstream_error_detail(exc) if exc is not None else "")
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
) -> dict[str, Any]:
    if inbound_format == "chat_completions":
        return _chat_completion_error_payload(
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
        )
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    error_detail = detail or (safe_upstream_error_detail(exc) if exc is not None else "")
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


def _upstream_format_candidates(upstream_format: str) -> tuple[str, ...]:
    if upstream_format == "chat_completions":
        return ("chat_completions",)
    if upstream_format == "anthropic_messages":
        raise ValueError("Anthropic Messages upstream is detected but Gateway /v1/messages conversion is not implemented yet")
    if upstream_format == "auto":
        return ("responses", "chat_completions")
    return ("responses",)


def _auto_protocol_fallback_allowed(exc: HTTPError) -> bool:
    return _upstream_retry_status(exc) in AUTO_UPSTREAM_PROTOCOL_FALLBACK_STATUSES


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


def _open_upstream_response(
    request: Request,
    *,
    upstream_name: str,
    upstream_format: str,
    timeout: int,
    event_context: Mapping[str, Any] | None = None,
    downstream_retry_callback: Any = None,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
    max_attempts: int | None = None,
    retry_policy: str = RETRY_GATEWAY_FULL,
    retry_http_errors: bool = True,
) -> Any:
    explicit_max_attempts = max_attempts is not None
    base_retry_attempts = _upstream_retry_attempts(request_kind) if max_attempts is None else max(1, max_attempts)
    retry_started_at = time.monotonic()
    attempt = 1
    while True:
        try:
            if upstream_name == "official":
                return _official_urlopen(request, timeout=timeout)
            return urlopen(request, timeout=timeout)
        except (HTTPError, IncompleteRead, OSError, URLError) as exc:
            if isinstance(exc, HTTPError) and not retry_http_errors:
                raise
            failure_class = _upstream_failure_class(exc)
            retry_attempts = _retry_attempts_for_failure_class(
                request_kind=request_kind,
                base_attempts=base_retry_attempts,
                failure_class=failure_class,
                explicit_max_attempts=explicit_max_attempts,
            )
            if attempt >= retry_attempts or failure_class == RETRY_FAILURE_PERMANENT:
                raise
            delay_seconds = gateway_retry_delay_seconds(attempt, failure_class=failure_class, exc=exc)
            if (
                failure_class in CAPACITY_RETRY_FAILURE_CLASSES
                and not _capacity_retry_elapsed_limit_allows(retry_started_at, delay_seconds)
            ):
                raise
            _emit_upstream_retry_event(
                event_context,
                upstream_name=upstream_name,
                upstream_format=upstream_format,
                request_kind=request_kind,
                attempt=attempt,
                max_attempts=retry_attempts,
                exc=exc,
                delay_seconds=delay_seconds,
                failure_class=failure_class,
            )
            if downstream_retry_callback is not None and retry_policy != RETRY_CONSERVATIVE_PRE_OUTPUT:
                downstream_retry_callback(
                    _downstream_retry_payload(
                        upstream_name=upstream_name,
                        upstream_format=upstream_format,
                        request_kind=request_kind,
                        attempt=attempt,
                        max_attempts=retry_attempts,
                        exc=exc,
                        delay_seconds=delay_seconds,
                        failure_class=failure_class,
                )
            )
            time.sleep(delay_seconds)
            attempt += 1


class CodexProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
            self._send_json(200, {"ok": True, "message": "shutdown scheduled"})
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

        self._send_json(404, {"error": "not found"})

    def _proxy_post_request(self, *, inbound_format: str, provider_hint: str | None = None) -> None:
        """Shared POST handler for inbound Responses and Chat Completions requests.

        ``inbound_format`` is the wire format the *caller* used.  When it is
        ``chat_completions`` the request body is converted to Responses format
        before routing, and the upstream response is converted back to Chat
        Completions format before being returned to the caller.
        """
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
        upstream_name = None
        upstream_format = "responses"
        reports_cached_input_tokens = False
        behavior_profile = None
        route_decision: RouteDecision | None = None
        route_policy_event_fields: dict[str, Any] = {}
        vision_proxy_policy = VISION_PROXY_DISABLED
        downstream_sse_started = False
        caller_body = b""
        caller_request_observability: dict[str, Any] = {}
        request_observability: dict[str, Any] = {}
        request_start_written = False
        write_request_start_once: Callable[[Mapping[str, Any]], None] | None = None

        try:
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
            route_decision = route_decision_for_request(
                upstream,
                request_context,
                inbound_format=inbound_format,
                provider_hint=provider_hint,
            )
            configured_upstream_format_for_route = str(upstream.get("upstream_format", "responses"))
            is_provider_transparent_metered = (
                provider_hint is not None
                and not _is_codex_app_context(request_context)
                and route_decision.behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                and (
                    (
                        inbound_format == "chat_completions"
                        and route_decision.selected_upstream_format == "chat_completions"
                        and route_decision.wire_format_adapter == WIRE_TRANSPARENT
                    )
                    or (
                        inbound_format == "chat_completions"
                        and configured_upstream_format_for_route == "responses"
                        and route_decision.selected_upstream_format == "responses"
                        and route_decision.wire_format_adapter == WIRE_CHAT_TO_RESPONSES
                    )
                    or (
                        inbound_format == "responses"
                        and route_decision.selected_upstream_format == "responses"
                        and route_decision.wire_format_adapter == WIRE_TRANSPARENT
                    )
                    or (
                        inbound_format == "responses"
                        and configured_upstream_format_for_route == "chat_completions"
                        and route_decision.selected_upstream_format == "chat_completions"
                        and route_decision.wire_format_adapter == WIRE_RESPONSES_TO_CHAT
                    )
                )
            )
            is_standard_third_party_transparent_metered = (
                provider_hint is None
                and upstream_name != "official"
                and not _is_codex_app_context(request_context)
                and _has_explicit_third_party_client_identity(request_context)
                and route_decision.behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                and (
                    (
                        inbound_format == "chat_completions"
                        and route_decision.selected_upstream_format == "chat_completions"
                        and route_decision.wire_format_adapter == WIRE_TRANSPARENT
                    )
                    or (
                        inbound_format == "chat_completions"
                        and configured_upstream_format_for_route == "responses"
                        and route_decision.selected_upstream_format == "responses"
                        and route_decision.wire_format_adapter == WIRE_CHAT_TO_RESPONSES
                    )
                    or (
                        inbound_format == "responses"
                        and route_decision.selected_upstream_format == "responses"
                        and route_decision.wire_format_adapter == WIRE_TRANSPARENT
                    )
                    or (
                        inbound_format == "responses"
                        and configured_upstream_format_for_route == "chat_completions"
                        and route_decision.selected_upstream_format == "chat_completions"
                        and route_decision.wire_format_adapter == WIRE_RESPONSES_TO_CHAT
                    )
                )
            )
            is_official_responses_transparent_metered = (
                provider_hint is None
                and upstream_name == "official"
                and not _is_codex_app_context(request_context)
                and request_context.get("client_id") != "unknown"
                and route_decision.behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                and route_decision.selected_upstream_format == "responses"
                and (
                    (
                        inbound_format == "responses"
                        and route_decision.wire_format_adapter == WIRE_TRANSPARENT
                    )
                    or (
                        inbound_format == "chat_completions"
                        and route_decision.wire_format_adapter == WIRE_CHAT_TO_RESPONSES
                    )
                )
            )
            enable_transparent_metered = (
                is_provider_transparent_metered
                or is_standard_third_party_transparent_metered
                or is_official_responses_transparent_metered
            )
            enable_codex_app_external_adapter = (
                route_decision.codex_semantic_adapter == CODEX_SEMANTIC_EXTERNAL_ADAPTER
            )
            behavior_profile = (
                route_decision.behavior_profile
                if enable_transparent_metered or enable_codex_app_external_adapter
                else behavior_profile_for_request(
                    upstream,
                    request_context,
                    inbound_format=inbound_format,
                )
            )
            upstream_format = route_decision.selected_upstream_format if enable_transparent_metered else upstream_format
            is_official_http_passthrough = behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
            is_transparent_metered = behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
            is_transparent_same_format = is_transparent_metered and route_decision.wire_format_adapter == WIRE_TRANSPARENT
            is_transparent_lightweight_fallback = (
                is_transparent_metered
                and route_decision.wire_format_adapter in {WIRE_CHAT_TO_RESPONSES, WIRE_RESPONSES_TO_CHAT}
            )
            if is_transparent_metered:
                request_kind = RETRY_REQUEST_MAIN_GENERATION
                proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
            vision_proxy_policy = vision_proxy_policy_for_route(route_decision, behavior_profile)
            route_policy_event_fields = {
                **_route_decision_event_fields(route_decision),
                "vision_proxy_policy": vision_proxy_policy,
            }
            proxy_request_context = {
                **proxy_request_context,
                **route_policy_event_fields,
            }
            model_canonical = canonical_model_id(model) if model else None
            if (
                request_kind == RETRY_REQUEST_COMPACT
                and not is_transparent_metered
                and behavior_profile != BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
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
            if isinstance(inbound_payload, Mapping):
                caller_stream = inbound_payload.get("stream") is True
            else:
                caller_stream = True
            prompt_cache_key = None
            if isinstance(inbound_payload, Mapping) and isinstance(inbound_payload.get("prompt_cache_key"), str):
                prompt_cache_key = inbound_payload["prompt_cache_key"]
            caller_request_observability = proxy_telemetry.enrich_request_observability(
                body=caller_body,
                codex_home=RUNTIME_CODEX_DIR,
                upstream=upstream,
                include_body_hmac=not is_official_http_passthrough,
                prompt_cache_key=prompt_cache_key,
                extract_prompt_cache_key=not is_official_http_passthrough,
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
            if inbound_format == "chat_completions" and not is_transparent_same_format:
                body = _chat_completions_request_to_responses_body(body)
            adapter_event_context = {
                "request_id": request_id,
                "model": model_canonical,
                "behavior_profile": behavior_profile,
                **proxy_request_context,
            }

            def emit_downstream_status(status_payload: Mapping[str, Any]) -> None:
                nonlocal downstream_sse_started
                if not caller_stream:
                    return
                if not downstream_sse_started:
                    self._send_sse_headers(200, upstream_name)
                    downstream_sse_started = True
                self._write_sse_data(
                    _downstream_stream_status_payload(inbound_format, status_payload, model_canonical)
            )

            usage_capture: dict[str, Any] = {}
            if is_official_http_passthrough:
                body = official_passthrough_request_body(
                    body,
                    inbound_payload,
                    upstream,
                    model_id=model,
                )
            elif is_transparent_same_format or is_transparent_lightweight_fallback:
                body = transparent_request_body(
                    body,
                    _safe_json_mapping(body),
                    upstream,
                    model_id=model,
                )
            else:
                compatibility_upstream = upstream
                if upstream_format == "auto":
                    compatibility_upstream = {**upstream, "upstream_format": "responses"}
                body = compatible_request_body(
                    body,
                    compatibility_upstream,
                    model_id=model,
                    event_context=adapter_event_context,
                    inject_codex_tools=request_kind != RETRY_REQUEST_COMPACT and not raw_provider_probe,
                    behavior_profile=behavior_profile,
                )
            vision_proxy_payload_format = (
                "chat_completions"
                if inbound_format == "chat_completions" and is_transparent_same_format
                else "responses"
            )
            inbound_has_image = isinstance(inbound_payload, Mapping) and _value_contains_image(inbound_payload)
            target_accepts_images = bool(model and model_supports_image(model, upstream))
            needs_image_payload_inspection = inbound_has_image and (
                vision_proxy_policy != VISION_PROXY_DISABLED or not target_accepts_images
            )
            image_proxy_payload: dict[str, Any] | None = None
            if needs_image_payload_inspection:
                try:
                    parsed_image_proxy_payload = json.loads(body.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_image_proxy_payload = None
                if isinstance(parsed_image_proxy_payload, dict):
                    image_proxy_payload = parsed_image_proxy_payload
            if image_proxy_payload is not None and vision_proxy_policy != VISION_PROXY_DISABLED:
                if apply_vision_proxy_adapter(
                    image_proxy_payload,
                    inbound_format=vision_proxy_payload_format,
                    target_model=model,
                    target_upstream=upstream,
                    vision_proxy_policy=vision_proxy_policy,
                    event_context=adapter_event_context,
                    progress_callback=emit_downstream_status if caller_stream else None,
                ):
                    body = json.dumps(image_proxy_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            if image_proxy_payload is not None and enforce_text_only_image_boundary(
                image_proxy_payload,
                inbound_format=vision_proxy_payload_format,
                target_model=model,
                target_upstream=upstream,
                event_context=adapter_event_context,
                progress_callback=emit_downstream_status if caller_stream else None,
            ):
                if vision_proxy_policy == VISION_PROXY_DISABLED:
                    vision_proxy_policy = VISION_PROXY_TRANSPARENT_OVERLAY
                    proxy_request_context = {
                        **proxy_request_context,
                        "vision_proxy_policy": vision_proxy_policy,
                    }
                    adapter_event_context = {
                        **adapter_event_context,
                        "vision_proxy_policy": vision_proxy_policy,
                    }
                body = json.dumps(image_proxy_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            responses_body = body
            headers = upstream_headers(
                self.headers,
                upstream,
                drop_content_encoding=content_decoded,
                behavior_profile=behavior_profile,
                model_id=model,
            )

            def upstream_body_for_format(selected_format: str) -> bytes:
                if is_transparent_same_format:
                    return body
                if selected_format == "chat_completions":
                    return _responses_request_to_chat_completion_body(responses_body)
                return responses_body

            upstream_request_observability = proxy_telemetry.enrich_request_observability(
                body=upstream_body_for_format(upstream_format),
                codex_home=RUNTIME_CODEX_DIR,
                upstream=upstream,
                include_body_hmac=not is_official_http_passthrough,
                prompt_cache_key=prompt_cache_key,
                extract_prompt_cache_key=not is_official_http_passthrough,
            )
            request_observability = {
                **upstream_request_observability,
                **_request_observability_with_prefix(caller_request_observability, "caller"),
                **_request_observability_with_prefix(upstream_request_observability, "upstream"),
            }
            emit_request_start_once(request_observability)
            emit_retry_to_downstream = (
                not is_official_http_passthrough
                and not is_transparent_metered
                and caller_stream
                and inbound_format == "responses"
                and gateway_downstream_retry_notice_enabled()
            )

            def upstream_request_for_format(
                selected_format: str,
                lifecycle_final_retry_reason: str | None = None,
            ) -> Request:
                request_body = body if is_transparent_same_format else responses_body
                if lifecycle_final_retry_reason and not is_transparent_same_format:
                    request_body = _responses_body_with_lifecycle_final_retry_guidance(
                        responses_body,
                        lifecycle_final_retry_reason,
                    )
                    _write_adapter_event(
                        adapter_event_context,
                        "lifecycle_final_retry_guidance_injected",
                        upstream=upstream_name,
                        upstream_format=selected_format,
                        reason=lifecycle_final_retry_reason,
                    )
                if is_transparent_same_format:
                    url = (
                        _chat_completions_url(upstream)
                        if selected_format == "chat_completions"
                        else _responses_url(upstream, "/v1/responses")
                    )
                    return Request(
                        url,
                        data=request_body,
                        headers=headers,
                        method="POST",
                    )
                if selected_format == "chat_completions":
                    return Request(
                        _chat_completions_url(upstream),
                        data=_responses_request_to_chat_completion_body(request_body),
                        headers=headers,
                        method="POST",
                    )
                return Request(
                    _responses_url(upstream, "/v1/responses"),
                    data=request_body,
                    headers=headers,
                    method="POST",
                )

            def emit_downstream_retry(payload: Mapping[str, Any]) -> None:
                nonlocal downstream_sse_started
                if not emit_retry_to_downstream:
                    return
                if not downstream_sse_started:
                    self._send_sse_headers(200, upstream_name)
                    downstream_sse_started = True
                self._write_sse_event("codexhub.retry", payload)
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

            def mark_downstream_sse_started() -> None:
                nonlocal downstream_sse_started
                downstream_sse_started = True

            configured_upstream_format = upstream_format
            selected_upstream_format = upstream_format
            upstream_format_options = _upstream_format_candidates(configured_upstream_format)
            for format_index, selected_upstream_format in enumerate(upstream_format_options):
                if isinstance(adapter_event_context, dict):
                    adapter_event_context["tool_protocol"] = _external_tool_protocol(
                        {**upstream, "upstream_format": selected_upstream_format}
                    )
                base_relay_attempts = (
                    OFFICIAL_PASSTHROUGH_FIRST_EVENT_ATTEMPTS
                    if is_official_http_passthrough
                    else _upstream_retry_attempts(request_kind)
                )
                relay_attempts = base_relay_attempts
                lifecycle_final_extra_attempts = (
                    1
                    if (not is_official_http_passthrough and lifecycle_empty_final_resample_enabled(adapter_event_context, request_kind))
                    else 0
                )
                max_relay_attempts = relay_attempts + lifecycle_final_extra_attempts
                relay_attempt = 1
                lifecycle_final_retry_reason: str | None = None
                try:
                    while relay_attempt <= max_relay_attempts:
                        request = upstream_request_for_format(
                            selected_upstream_format,
                            lifecycle_final_retry_reason,
                        )
                        try:
                            with _open_upstream_response(
                                request,
                                upstream_name=upstream_name,
                                upstream_format=selected_upstream_format,
                                timeout=upstream_timeout_seconds(),
                                event_context=adapter_event_context,
                                downstream_retry_callback=emit_downstream_retry if emit_retry_to_downstream else None,
                                request_kind=request_kind,
                                max_attempts=official_upstream_open_attempts() if is_official_http_passthrough else None,
                                retry_policy=route_decision.retry_policy
                                if enable_transparent_metered
                                else RETRY_GATEWAY_FULL,
                                retry_http_errors=not is_official_http_passthrough,
                            ) as response:
                                status = self._relay_upstream_response(
                                    response,
                                    upstream_name,
                                    request_id=request_id,
                                    model=model_canonical,
                                    upstream_format=selected_upstream_format,
                                    inbound_format=inbound_format,
                                    caller_stream=caller_stream,
                                    event_context=adapter_event_context,
                                    usage_capture=usage_capture,
                                    headers_already_sent=downstream_sse_started,
                                    request_kind=request_kind,
                                    defer_stream_errors=relay_attempt < relay_attempts,
                                    mark_downstream_sse_started=mark_downstream_sse_started,
                                    behavior_profile=behavior_profile,
                                )
                            break
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
                            lifecycle_retry = isinstance(
                                exc,
                                (LifecycleEmptyFinalResponseError, LifecycleFinalFormatResponseError),
                            )
                            if lifecycle_retry:
                                retry_exc: BaseException = exc
                                failure_class = RETRY_FAILURE_QUICK_TRANSIENT
                                lifecycle_final_retry_reason = "empty" if isinstance(exc, LifecycleEmptyFinalResponseError) else "format"
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
                                retry_exc = exc.cause if isinstance(exc, UpstreamStreamInterruptedError) else exc
                                failure_class = _upstream_failure_class(retry_exc)
                                relay_attempts = _retry_attempts_for_failure_class(
                                    request_kind=request_kind,
                                    base_attempts=base_relay_attempts,
                                    failure_class=failure_class,
                                    explicit_max_attempts=False,
                                    stream_failure=stream_failure,
                                )
                                if isinstance(retry_exc, UpstreamEmptyCompletedResponseError):
                                    relay_attempts = min(relay_attempts, 2)
                                max_relay_attempts = relay_attempts + lifecycle_final_extra_attempts
                                retry_limit = relay_attempts
                                if relay_attempt >= retry_limit or failure_class == RETRY_FAILURE_PERMANENT:
                                    raise retry_exc
                                delay_seconds = gateway_retry_delay_seconds(
                                    relay_attempt,
                                    failure_class=failure_class,
                                    exc=retry_exc,
                                )
                                if failure_class in CAPACITY_RETRY_FAILURE_CLASSES and not _capacity_retry_elapsed_limit_allows(
                                    started_at,
                                    delay_seconds,
                                ):
                                    raise retry_exc
                                if (
                                    stream_failure
                                    and failure_class == RETRY_FAILURE_QUICK_TRANSIENT
                                    and not _stream_retry_elapsed_limit_allows(started_at, delay_seconds)
                                ):
                                    raise retry_exc
                            _emit_upstream_retry_event(
                                adapter_event_context,
                                upstream_name=upstream_name,
                                upstream_format=selected_upstream_format,
                                request_kind=request_kind,
                                attempt=relay_attempt,
                                max_attempts=retry_limit,
                                exc=retry_exc,
                                delay_seconds=delay_seconds,
                                failure_class=failure_class,
                                failure_phase="stream_body" if stream_failure else None,
                            )
                            emit_downstream_retry(
                                _downstream_retry_payload(
                                    upstream_name=upstream_name,
                                    upstream_format=selected_upstream_format,
                                    request_kind=request_kind,
                                    attempt=relay_attempt,
                                    max_attempts=retry_limit,
                                    exc=retry_exc,
                                    delay_seconds=delay_seconds,
                                    failure_class=failure_class,
                                    failure_phase="stream_body" if stream_failure else None,
                                )
                            )
                            time.sleep(delay_seconds)
                            relay_attempt += 1
                            continue
                        relay_attempt += 1
                    else:
                        raise RuntimeError("unreachable upstream relay retry state")
                    upstream_format = selected_upstream_format
                    break
                except HTTPError as exc:
                    next_format_available = format_index + 1 < len(upstream_format_options)
                    if (
                        configured_upstream_format == "auto"
                        and selected_upstream_format == "responses"
                        and next_format_available
                        and not downstream_sse_started
                        and _auto_protocol_fallback_allowed(exc)
                    ):
                        write_proxy_event(
                            "upstream_protocol_fallback",
                            request_id=request_id,
                            model=model_canonical,
                            model_requested=model_requested,
                            model_canonical=model_canonical,
                            upstream=upstream_name,
                            provider_id=upstream_name,
                            provider_hint=provider_hint,
                            upstream_format=configured_upstream_format,
                            behavior_profile=behavior_profile,
                            failed_upstream_format=selected_upstream_format,
                            next_upstream_format=upstream_format_options[format_index + 1],
                            status=getattr(exc, "code", 502),
                            error="HTTPError",
                            detail=safe_upstream_error_detail(exc),
                            **proxy_request_context,
                        )
                        continue
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
        except CompactEmptyResponseError as exc:
            detail = safe_upstream_error_detail(exc)
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
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=502,
                    exc=exc,
                    error="compact_empty_response",
                    detail=detail,
                )
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                error="compact_empty_response",
                detail=detail,
                error_type="compact_empty_response",
            )
        except (LifecycleEmptyFinalResponseError, LifecycleFinalFormatResponseError) as exc:
            detail = safe_upstream_error_detail(exc)
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
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=502,
                    exc=exc,
                    error=error_code,
                    detail=detail,
                )
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                error=error_code,
                detail=detail,
                error_type=error_code,
            )
        except ImageProxyError as exc:
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
                detail=str(exc)[:300],
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            if downstream_sse_started:
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=502,
                    exc=exc,
                    error="image_proxy_error",
                    detail=str(exc),
                )
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                error="image_proxy_error",
                detail=str(exc),
                error_type="image_proxy_error",
            )
        except ValueError as exc:
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
                detail=str(exc)[:300],
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
                detail=str(exc),
                error_type="invalid_request_error",
            )
        except HTTPError as exc:
            if downstream_sse_started:
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=getattr(exc, "code", 502),
                    exc=exc,
                )
                write_proxy_event(
                    "request_error",
                    request_id=request_id,
                    model=canonical_model_id(model) if model else None,
                    upstream=upstream_name,
                    upstream_format=upstream_format,
                    behavior_profile=behavior_profile,
                    status=getattr(exc, "code", 502),
                    error="HTTPError",
                    detail=safe_upstream_error_detail(exc),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **proxy_request_context,
                )
                return
            try:
                adapter_event_context = {
                    "request_id": request_id,
                    "model": canonical_model_id(model) if model else None,
                    "behavior_profile": behavior_profile,
                    **proxy_request_context,
                }
                status = self._relay_upstream_response(
                    exc,
                    upstream_name or "upstream_error",
                    request_id=request_id,
                    model=canonical_model_id(model) if model else None,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    caller_stream=caller_stream,
                    event_context=adapter_event_context,
                    usage_capture=usage_capture,
                    behavior_profile=behavior_profile,
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
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                status=status,
                error="HTTPError",
                detail=safe_upstream_error_detail(exc),
                failure_phase=transport_failure_phase(exc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
        except IncompleteRead as exc:
            detail = safe_upstream_error_detail(exc)
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
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    exc=exc,
                )
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
            )
        except (OSError, URLError) as exc:
            detail = safe_upstream_error_detail(exc)
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
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    exc=exc,
                )
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
            )
        except Exception as exc:
            detail = safe_upstream_error_detail(exc)
            logger.exception("unexpected proxy error request_id=%s", request_id)
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
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=500,
                    exc=exc,
                )
                return
            self._safe_send_downstream_json_error(
                500,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
            )

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
                status = self._relay_upstream_response(response, upstream_name, request_id=request_id, model=None)
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
                status = self._relay_upstream_response(exc, upstream_name, request_id=request_id, model=None)
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

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_response_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self, status: int, upstream_name: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Codex-Proxy-Upstream", upstream_name)
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_sse_event(self, event: str, payload: Mapping[str, Any]) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(
            b"data: "
            + json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            + b"\n\n"
        )
        self.wfile.flush()

    def _write_sse_data(self, payload: Mapping[str, Any]) -> None:
        self.wfile.write(_sse_json_line(payload, b"\n") + b"\n")
        self.wfile.flush()

    def _write_sse_keepalive(self) -> None:
        self.wfile.write(b": codexhub.keepalive\n\n")
        self.wfile.flush()

    def _iter_upstream_sse_lines(
        self,
        response: Any,
        *,
        downstream_output_started: Callable[[], bool] | None = None,
        line_resets_idle_timeout: Callable[[bytes], bool] | None = None,
    ) -> Any:
        keepalive_interval = sse_keepalive_seconds()
        transport_timeout_seconds = transport_sse_idle_timeout_seconds()
        model_event_timeout_seconds = model_event_sse_idle_timeout_seconds()
        transport_idle_guard_enabled = transport_timeout_seconds > 0
        model_event_idle_guard_enabled = model_event_timeout_seconds > 0 and line_resets_idle_timeout is not None
        if keepalive_interval <= 0 and not transport_idle_guard_enabled and not model_event_idle_guard_enabled:
            while True:
                line = response.readline()
                yield line
                if not line:
                    return

        lines: queue.Queue[tuple[str, bytes | BaseException]] = queue.Queue()

        def read_upstream_lines() -> None:
            try:
                while True:
                    line = response.readline()
                    lines.put(("line", line))
                    if not line:
                        return
            except BaseException as exc:
                lines.put(("error", exc))

        threading.Thread(target=read_upstream_lines, name="codex-proxy-sse-reader", daemon=True).start()
        stream_started_at = time.monotonic()
        last_transport_at = stream_started_at
        last_model_event_at = stream_started_at
        last_keepalive_at = stream_started_at

        def close_response_for_idle_timeout() -> None:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        def raise_idle_timeout(timeout_seconds: float, phase: str) -> None:
            close_response_for_idle_timeout()
            raise UpstreamStreamIdleTimeoutError(timeout_seconds, phase=phase)

        while True:
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

            try:
                if timeout_seconds is None:
                    kind, value = lines.get()
                else:
                    kind, value = lines.get(timeout=timeout_seconds)
            except queue.Empty:
                now = time.monotonic()
                if transport_idle_guard_enabled and (now - last_transport_at) >= transport_timeout_seconds:
                    raise_idle_timeout(transport_timeout_seconds, "transport")
                if model_event_idle_guard_enabled and (now - last_model_event_at) >= model_event_timeout_seconds:
                    raise_idle_timeout(model_event_timeout_seconds, "model_event")
                if keepalive_interval > 0:
                    self._write_sse_keepalive()
                    last_keepalive_at = time.monotonic()
                continue
            if kind == "error":
                raise value
            if isinstance(value, bytes) and value:
                now = time.monotonic()
                last_transport_at = now
                if model_event_idle_guard_enabled and line_resets_idle_timeout is not None and line_resets_idle_timeout(value):
                    last_model_event_at = now
            yield value
            if not value:
                return

    def _write_sse_error_event(self, upstream_name: str, exc: BaseException) -> None:
        self._write_sse_event(
            "error",
            _downstream_stream_error_payload(upstream_name=upstream_name, exc=exc),
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
    ) -> None:
        error_spec = DownstreamErrorSpec(
            inbound_format=inbound_format,
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
        )
        if inbound_format == "chat_completions":
            self.wfile.write(
                b"data: "
                + json.dumps(
                    _downstream_sse_error_payload_for_inbound_format(error_spec),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n\n"
            )
            self.wfile.flush()
            self.close_connection = True
            return
        if exc is not None:
            self._write_sse_event("error", _downstream_sse_error_payload_for_inbound_format(error_spec))
            self.close_connection = True
            return
        self._write_sse_event("error", _downstream_sse_error_payload_for_inbound_format(error_spec))
        self.close_connection = True

    def _write_sse_protocol_error_event(
        self,
        upstream_name: str,
        status: int,
        detail: str,
        *,
        error: str = "UpstreamProtocolError",
    ) -> None:
        self._write_sse_event(
            "error",
            _downstream_stream_error_payload(
                upstream_name=upstream_name,
                status=status,
                error=error,
                detail=detail,
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
    ) -> None:
        error_spec = DownstreamErrorSpec(
            inbound_format=inbound_format,
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
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

        def send_downstream_headers_once() -> None:
            nonlocal headers_sent_downstream
            if headers_sent_downstream:
                return
            self.send_response(status)
            for key, value in _filtered_response_headers(response.headers, True):
                self.send_header(key, value)
            self.send_header("X-Codex-Proxy-Upstream", upstream_name)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent_downstream = True
            if mark_downstream_sse_started is not None:
                mark_downstream_sse_started()

        if not defer_stream_errors:
            send_downstream_headers_once()

        usage_context = {
            "request_id": request_id,
            "model": model,
            "upstream": upstream_name,
            "upstream_format": upstream_format,
            "inbound_format": inbound_format,
        }
        _capture_usage(usage_capture, None, missing_reason="async_official_passthrough")
        lines_streamed = 0
        bytes_streamed = 0
        last_upstream_byte_at: float | None = None
        failure_side = "upstream_read"
        sse_stats = PassthroughSseSemanticStats()
        terminal_drain_timeout_shortened = False
        try:
            while True:
                failure_side = "upstream_read"
                line = response.readline()
                if not line:
                    if defer_stream_errors and not headers_sent_downstream:
                        raise UpstreamStreamIncompleteError("Official stream ended before its first SSE byte")
                    break
                send_downstream_headers_once()
                last_upstream_byte_at = time.monotonic()
                failure_side = "downstream_write"
                self.wfile.write(line)
                self.wfile.flush()
                lines_streamed += 1
                bytes_streamed += len(line)
                sse_stats.observe_line(line)
                _offer_official_passthrough_usage_line(usage_context, line)
                if sse_stats.terminal_event_seen and not terminal_drain_timeout_shortened:
                    shorten_terminal_drain_timeout = getattr(response, "shorten_terminal_drain_timeout", None)
                    if callable(shorten_terminal_drain_timeout):
                        shorten_terminal_drain_timeout(OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS)
                    terminal_drain_timeout_shortened = True
        except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
            if defer_stream_errors and not headers_sent_downstream:
                raise UpstreamStreamInterruptedError(exc) from exc
            self.close_connection = True
            if failure_side == "upstream_read" and sse_stats.terminal_event_seen:
                sse_stats.finalize_pending()
                if usage_capture is not None:
                    usage_capture.update(sse_stats.fields())
                return status
            now = time.monotonic()
            last_upstream_byte_age_ms = (
                None
                if last_upstream_byte_at is None
                else int(max(0.0, now - last_upstream_byte_at) * 1000)
            )
            failure_phase = "downstream_write" if failure_side == "downstream_write" else "stream_body"
            client_disconnected = failure_side == "downstream_write"
            telemetry_status = 499 if client_disconnected else 502
            synthetic_terminal_event_sent = False
            synthetic_terminal_write_error: str | None = None
            synthetic_terminal_write_detail: str | None = None
            if not client_disconnected:
                try:
                    if sse_stats.has_pending_event():
                        self.wfile.write(b"\n")
                        self.wfile.flush()
                        sse_stats.finalize_pending()
                    self._write_sse_event(
                        "response.failed",
                        _responses_failed_event_for_stream_error(
                            upstream_name=upstream_name,
                            model=model,
                            status=502,
                            exc=exc,
                            response_id=sse_stats.response_id,
                        ),
                    )
                    synthetic_terminal_event_sent = True
                except OSError as write_exc:
                    synthetic_terminal_write_error = type(write_exc).__name__
                    synthetic_terminal_write_detail = safe_upstream_error_detail(write_exc)
            sse_fields = sse_stats.fields()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
                usage_capture["synthetic_terminal_event_sent"] = synthetic_terminal_event_sent
                if synthetic_terminal_event_sent:
                    usage_capture["synthetic_terminal_event_type"] = "response.failed"
                if synthetic_terminal_write_error is not None:
                    usage_capture["synthetic_terminal_write_error"] = synthetic_terminal_write_error
            write_proxy_event(
                "official_passthrough_stream_closed",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=telemetry_status,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                failure_phase=failure_phase,
                failure_side=failure_side,
                failure_class="downstream_client_closed" if client_disconnected else "upstream_stream_interrupted",
                client_disconnected=client_disconnected,
                synthetic_terminal_event_sent=synthetic_terminal_event_sent,
                synthetic_terminal_event_type="response.failed" if synthetic_terminal_event_sent else None,
                synthetic_terminal_write_error=synthetic_terminal_write_error,
                synthetic_terminal_write_detail=synthetic_terminal_write_detail,
                lines_streamed=lines_streamed,
                bytes_streamed=bytes_streamed,
                last_upstream_byte_age_ms=last_upstream_byte_age_ms,
                headers_sent_downstream=headers_sent_downstream,
                downstream_sse_started=True,
                **sse_fields,
            )
            return telemetry_status

        self.close_connection = True
        sse_stats.finalize_pending()
        if usage_capture is not None:
            usage_capture.update(sse_stats.fields())
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

        headers_sent = headers_already_sent

        def send_downstream_headers_once() -> None:
            nonlocal headers_sent
            if headers_sent:
                return
            self.send_response(status)
            for key, value in _filtered_response_headers(response.headers, is_event_stream):
                self.send_header(key, value)
            self.send_header("X-Codex-Proxy-Upstream", upstream_name)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            if is_event_stream and mark_downstream_sse_started is not None:
                mark_downstream_sse_started()

        if not (defer_stream_errors and is_event_stream and not headers_already_sent):
            send_downstream_headers_once()

        _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
        if is_event_stream:
            pending_lines: list[bytes] = []

            def transparent_error_event(payload: Mapping[str, Any]) -> UpstreamStreamErrorEvent | None:
                if upstream_format == "responses" and _responses_stream_error_type(payload) is not None:
                    return UpstreamStreamErrorEvent(payload)
                if upstream_format == "chat_completions" and _chat_stream_error_detail(payload) is not None:
                    return UpstreamStreamErrorEvent(payload)
                return None

            def write_transparent_line(line: bytes) -> None:
                self.wfile.write(line)
                self.wfile.flush()
                _offer_usage_observed_sse_line(
                    usage_context,
                    line,
                    upstream_format=upstream_format,
                )

            while True:
                try:
                    line = response.readline()
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors and not headers_sent:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    self.close_connection = True
                    write_proxy_event(
                        "transparent_stream_closed",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        error=type(exc).__name__,
                        detail=safe_upstream_error_detail(exc),
                    )
                    return 502
                if not line:
                    break
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
                    send_downstream_headers_once()
                    try:
                        for pending_line in pending_lines:
                            write_transparent_line(pending_line)
                    except OSError as exc:
                        self.close_connection = True
                        event_fields = dict(event_context or {})
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
                            status=status,
                            upstream_format=upstream_format,
                            inbound_format=inbound_format,
                            error=type(exc).__name__,
                            detail=safe_upstream_error_detail(exc),
                            **event_fields,
                        )
                        return status
                    pending_lines.clear()
                    continue
                try:
                    send_downstream_headers_once()
                    write_transparent_line(line)
                except OSError as exc:
                    self.close_connection = True
                    event_fields = dict(event_context or {})
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
                        status=status,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                        error=type(exc).__name__,
                        detail=safe_upstream_error_detail(exc),
                        **event_fields,
                    )
                    return status
            if pending_lines and not headers_sent:
                send_downstream_headers_once()
                for pending_line in pending_lines:
                    write_transparent_line(pending_line)
            self.close_connection = True
            return status

        body = b""
        try:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                body += chunk
        except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
            self.close_connection = True
            write_proxy_event(
                "transparent_body_read_failed",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=502,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
            )
            return 502

        self.wfile.write(body)
        self.wfile.flush()
        _offer_usage_observed_body(usage_context, body)
        self.close_connection = True
        return status

    def _relay_upstream_response(
        self,
        response: Any,
        upstream_name: str,
        request_id: str | None = None,
        model: str | None = None,
        upstream_format: str = "responses",
        inbound_format: str = "responses",
        caller_stream: bool = True,
        event_context: Mapping[str, Any] | None = None,
        usage_capture: dict[str, Any] | None = None,
        headers_already_sent: bool = False,
        request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
        defer_stream_errors: bool = False,
        mark_downstream_sse_started: Callable[[], None] | None = None,
        behavior_profile: str | None = None,
    ) -> int:
        status = getattr(response, "status", None) or getattr(response, "code", 502)
        is_event_stream = _is_event_stream(response.headers)
        # When the caller spoke Chat Completions, the response must be converted
        # back to Chat Completions format regardless of the upstream wire format.
        want_chat_output = inbound_format == "chat_completions"
        # When the caller asked for a non-streaming response but the upstream
        # returns SSE (e.g. chatgpt.com forces stream=true), buffer the entire
        # SSE into a single JSON response body.
        buffer_sse_to_json = is_event_stream and not caller_stream
        buffered_json_response = False
        usage_context = _usage_observed_context(
            event_context,
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
        )
        if (
            behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
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
            behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
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
            and lifecycle_empty_final_resample_enabled(event_context, request_kind)
        )
        headers_sent = headers_already_sent
        if not is_event_stream or buffer_sse_to_json:
            if buffer_sse_to_json:
                # Buffer the full SSE stream into a list of events.
                events: list[Mapping[str, Any]] = []
                try:
                    while True:
                        line = response.readline()
                        if not line:
                            break
                        payload_bytes = _sse_payload_bytes(line)
                        if payload_bytes is None:
                            continue
                        try:
                            event = json.loads(payload_bytes.decode("utf-8-sig"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if isinstance(event, dict):
                            events.append(event)
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    raise
                # Reconstruct a Responses-format body from the events.
                try:
                    body = _events_to_responses_body(events, require_completed=True)
                except UpstreamStreamIncompleteError:
                    if defer_stream_errors:
                        raise
                    status = 502
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
            if want_chat_output:
                if upstream_format == "chat_completions":
                    body = _response_body_to_chat_completion_body(
                        compatible_response_body(
                            _chat_completion_to_response_body(body),
                            upstream_name,
                            event_context=event_context,
                        )
                    )
                else:
                    # Upstream returned Responses format; convert to Chat Completions.
                    if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
                        body = _response_body_to_chat_completion_body(body)
                    else:
                        body = _response_body_to_chat_completion_body(
                            compatible_response_body(body, upstream_name, event_context=event_context)
                        )
            elif upstream_format == "chat_completions":
                converted_body = _chat_completion_to_response_body(
                    body,
                    repair=behavior_profile != BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
                )
                if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
                    body = converted_body
                else:
                    body = compatible_response_body(
                        converted_body,
                        upstream_name,
                        event_context=event_context,
                    )
            else:
                body = compatible_response_body(body, upstream_name, event_context=event_context)
            if status >= 400:
                body = _with_codexhub_http_error(
                    body,
                    upstream_name=upstream_name,
                    status=status,
                    exc=response if isinstance(response, BaseException) else None,
                )
            if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
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
                    ),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                event_fields = dict(event_context or {})
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
                    event_fields = dict(event_context) if event_context else {}
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
                        self._send_sse_headers(status, upstream_name)
                        headers_sent = True
                        if mark_downstream_sse_started is not None:
                            mark_downstream_sse_started()
                    for event in response_events:
                        if behavior_profile != BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
                            event, _ = _normalize_third_party_tool_call(event)
                            event, _ = _downgrade_invalid_third_party_tool_calls(event)
                            event, _ = _guard_duplicate_multi_agent_spawn_calls(event, event_context)
                        event_type = event.get("type")
                        if isinstance(event_type, str) and event_type:
                            self._write_sse_event(event_type, event)
                    self.close_connection = True
                    _capture_usage(
                        usage_capture,
                        None,
                        missing_reason="async_usage_pending"
                        if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                        else "upstream_missing_usage",
                    )
                    return status
            if headers_sent:
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=status,
                    error="UpstreamProtocolError",
                    detail=f"upstream returned non-SSE response after downstream SSE retry status: HTTP {status}",
                )
                self.close_connection = True
                _capture_usage(usage_capture, None, missing_reason="stream_protocol_error")
                return status

        def send_downstream_response_headers_once() -> None:
            nonlocal headers_sent
            if headers_sent:
                return
            self.send_response(status)
            content_length = None if is_event_stream else len(body)
            content_type = "application/json" if buffered_json_response else None
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
            headers_sent = True
            if mark_downstream_sse_started is not None:
                mark_downstream_sse_started()

        if not defer_stream_headers:
            send_downstream_response_headers_once()

        if is_event_stream:
            def finish_downstream_stream_closed(exc: OSError) -> int:
                self.close_connection = True
                event_fields = dict(event_context or {})
                for key in ("request_id", "model", "upstream", "status", "error", "detail"):
                    event_fields.pop(key, None)
                write_proxy_event(
                    "downstream_stream_closed",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=status,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    error=type(exc).__name__,
                    detail=safe_upstream_error_detail(exc),
                    **event_fields,
                )
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="async_usage_pending"
                    if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                    else "client_disconnected",
                )
                return status

            if (
                behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                and want_chat_output
                and upstream_format != "chat_completions"
            ):
                line_ending = b"\n"
                converter = _ResponsesToChatStreamConverter()
                try:
                    for line in self._iter_upstream_sse_lines(
                        response,
                        line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
                    ):
                        if not line:
                            break
                        line_ending = _sse_line_ending(line)
                        payload_bytes = _sse_payload_bytes(line)
                        if payload_bytes is None:
                            continue
                        if payload_bytes == b"[DONE]":
                            continue
                        try:
                            event = json.loads(payload_bytes.decode("utf-8-sig"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(event, Mapping):
                            continue
                        _offer_usage_observed_sse_line(
                            usage_context,
                            line,
                            upstream_format=upstream_format,
                        )
                        error_type = _responses_stream_error_type(event)
                        if error_type is not None:
                            detail = _responses_stream_error_detail(event)
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
                            self._write_downstream_sse_error(
                                inbound_format=inbound_format,
                                upstream_name=upstream_name,
                                status=502,
                                error=error_type,
                                detail=detail,
                            )
                            _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                            return 502
                        for chunk in converter.chunks_for_event(event):
                            self.wfile.write(
                                b"data: "
                                + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
                                + b"\n\n"
                            )
                            self.wfile.flush()
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
                        detail=safe_upstream_error_detail(exc),
                    )
                    send_downstream_response_headers_once()
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=safe_upstream_error_detail(exc),
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=safe_upstream_error_detail(exc),
                    )
                    send_downstream_response_headers_once()
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                if not converter.completed:
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
                    send_downstream_response_headers_once()
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream stream ended before response.completed.",
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
                _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
                return status

            if (
                behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                and upstream_format == "chat_completions"
                and not want_chat_output
            ):
                line_ending = b"\n"
                converter = _ChatToResponsesStreamConverter()
                try:
                    for line in self._iter_upstream_sse_lines(
                        response,
                        line_resets_idle_timeout=_chat_sse_line_resets_idle_timeout,
                    ):
                        if not line:
                            break
                        line_ending = _sse_line_ending(line)
                        payload_bytes = _sse_payload_bytes(line)
                        if payload_bytes is None:
                            continue
                        events: list[dict[str, Any]] = []
                        if payload_bytes == b"[DONE]":
                            events = converter.events_for_done()
                        else:
                            try:
                                payload = json.loads(payload_bytes.decode("utf-8-sig"))
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                continue
                            if not isinstance(payload, Mapping):
                                continue
                            chat_error_detail = _chat_stream_error_detail(payload)
                            if chat_error_detail is not None:
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
                                self._write_downstream_sse_error(
                                    inbound_format=inbound_format,
                                    upstream_name=upstream_name,
                                    status=502,
                                    error="chat_completions_error",
                                    detail=chat_error_detail,
                                )
                                _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                                return 502
                            _offer_usage_observed_sse_line(
                                usage_context,
                                line,
                                upstream_format=upstream_format,
                            )
                            events = converter.events_for_chunk(payload)
                        for event in events:
                            try:
                                self.wfile.write(_sse_json_line(event, line_ending) + line_ending)
                                self.wfile.flush()
                            except OSError as exc:
                                return finish_downstream_stream_closed(exc)
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
                        detail=safe_upstream_error_detail(exc),
                    )
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=safe_upstream_error_detail(exc),
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=safe_upstream_error_detail(exc),
                    )
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                if not converter.completed:
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
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream Chat Completions stream ended without finish_reason or [DONE].",
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError as exc:
                    return finish_downstream_stream_closed(exc)
                self.close_connection = True
                _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
                return status

            if want_chat_output and upstream_format != "chat_completions":
                # Upstream returns Responses SSE; convert to Chat Completions SSE.
                line_ending = b"\n"
                events: list[Mapping[str, Any]] = []
                try:
                    for line in self._iter_upstream_sse_lines(
                        response,
                        line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
                    ):
                        if not line:
                            break
                        line_ending = _sse_line_ending(line)
                        payload_bytes = _sse_payload_bytes(line)
                        if payload_bytes is None:
                            continue
                        try:
                            event = json.loads(payload_bytes.decode("utf-8-sig"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if isinstance(event, dict):
                            events.append(event)
                            if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
                                _offer_usage_observed_sse_line(
                                    usage_context,
                                    line,
                                    upstream_format=upstream_format,
                                )
                            else:
                                _capture_usage(usage_capture, _usage_from_response_event(event))
                except UpstreamStreamIdleTimeoutError as exc:
                    if defer_stream_errors:
                        raise
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
                        detail=safe_upstream_error_detail(exc),
                    )
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=safe_upstream_error_detail(exc),
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=safe_upstream_error_detail(exc),
                    )
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                try:
                    response_body = compatible_response_body(
                        _events_to_responses_body(events, require_completed=True),
                        upstream_name,
                        event_context=event_context,
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
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream stream ended before response.completed.",
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502

                send_downstream_response_headers_once()
                for chunk in _chat_completion_body_to_stream_chunks(
                    _response_body_to_chat_completion_body(response_body)
                ):
                    self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="async_usage_pending"
                    if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                    else "upstream_missing_usage",
                )
                return status

            if upstream_format == "chat_completions":
                line_ending = b"\n"
                chunks: list[Mapping[str, Any] | str] = []
                try:
                    for line in self._iter_upstream_sse_lines(
                        response,
                        line_resets_idle_timeout=_chat_sse_line_resets_idle_timeout,
                    ):
                        if not line:
                            break
                        line_ending = _sse_line_ending(line)
                        payload_bytes = _sse_payload_bytes(line)
                        if payload_bytes is None:
                            continue
                        if payload_bytes == b"[DONE]":
                            chunks.append("[DONE]")
                            continue
                        try:
                            payload = json.loads(payload_bytes.decode("utf-8-sig"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if isinstance(payload, dict):
                            chunks.append(payload)
                            if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED:
                                _offer_usage_observed_sse_line(
                                    usage_context,
                                    line,
                                    upstream_format=upstream_format,
                                )
                            else:
                                _capture_usage(usage_capture, _usage_from_payload(payload))
                except UpstreamStreamIdleTimeoutError as exc:
                    if defer_stream_errors:
                        raise
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
                        detail=safe_upstream_error_detail(exc),
                    )
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=safe_upstream_error_detail(exc),
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    if defer_stream_errors:
                        raise UpstreamStreamInterruptedError(exc) from exc
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=safe_upstream_error_detail(exc),
                    )
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
                if not _chat_stream_chunks_have_terminal(chunks):
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
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream Chat Completions stream ended without finish_reason or [DONE].",
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                    return 502
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
                        event_context=event_context,
                    )
                    send_downstream_response_headers_once()
                    for chunk in _chat_completion_body_to_stream_chunks(
                        _response_body_to_chat_completion_body(response_body)
                    ):
                        self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
                        self.wfile.flush()
                else:
                    events = _chat_stream_chunks_to_response_events(chunks)
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
                    events, _ = _normalize_third_party_tool_call(events)
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
                    events, _ = _coerce_exact_spawn_prompt_tool_calls(events, event_context)
                    events, _ = _coerce_required_subagent_tool_calls(events, event_context)
                    events, _ = _reconcile_function_call_argument_events(events)
                    events, _ = _repair_missing_required_subagent_call_events(events, event_context)
                    _write_adapter_event(
                        event_context,
                        "chat_to_responses_event_summary",
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        stage="final",
                        **_response_events_shape_summary(events),
                    )
                    send_downstream_response_headers_once()
                    for event in events:
                        self.wfile.write(_sse_json_line(event, line_ending) + line_ending)
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="async_usage_pending"
                    if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                    else "upstream_missing_usage",
                )
                return status

            if lifecycle_empty_final_resample_enabled(event_context, request_kind):
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
                try:
                    for line in self._iter_upstream_sse_lines(
                        response,
                        line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
                    ):
                        if not line:
                            break
                        original_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                        usage_payload = _parse_sse_json_payload(line)
                        if isinstance(usage_payload, Mapping):
                            event_type = usage_payload.get("type")
                            if isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error"):
                                saw_response_event = True
                            if _responses_events_have_terminal([usage_payload]):
                                saw_terminal_event = True
                            if _responses_event_starts_downstream_output(usage_payload):
                                downstream_output_started = True
                            _capture_usage(usage_capture, _usage_from_response_event(usage_payload))
                        rewritten_line = compatible_sse_line(line, upstream_name, event_context=event_context)
                        rewritten_payload = _parse_sse_json_payload(rewritten_line) if upstream_name != "official" else usage_payload
                        _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payload)
                        if isinstance(rewritten_payload, Mapping):
                            rewritten_events.append(rewritten_payload)
                        terminal = bool(
                            isinstance(rewritten_payload, Mapping)
                            and _responses_events_have_terminal([rewritten_payload])
                        )
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
                        detail=safe_upstream_error_detail(exc),
                    )
                    send_downstream_response_headers_once()
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_idle_timeout",
                        detail=safe_upstream_error_detail(exc),
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                    return 502
                except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                    self.close_connection = True
                    write_proxy_event(
                        "upstream_stream_interrupted",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=502,
                        error=type(exc).__name__,
                        detail=safe_upstream_error_detail(exc),
                    )
                    send_downstream_response_headers_once()
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        exc=exc,
                    )
                    _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                    return 502
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
                    send_downstream_response_headers_once()
                    self._write_downstream_sse_error(
                        inbound_format=inbound_format,
                        upstream_name=upstream_name,
                        status=502,
                        error="upstream_stream_incomplete",
                        detail="Upstream Responses stream ended without a terminal event.",
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
                send_downstream_response_headers_once()
                for buffered_line, terminal in buffered_lines:
                    self.wfile.write(buffered_line)
                    if terminal:
                        separator = _sse_event_separator_after_line(buffered_line)
                        if separator:
                            self.wfile.write(separator)
                    self.wfile.flush()
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

            def write_or_queue_downstream_line(out_line: bytes, *, buffer: bool = False, force: bool = False) -> None:
                if not out_line:
                    return
                if buffer and not force:
                    pending_downstream_lines.append(out_line)
                    return
                if pending_downstream_lines:
                    for pending_line in pending_downstream_lines:
                        self.wfile.write(pending_line)
                    pending_downstream_lines.clear()
                self.wfile.write(out_line)
                self.wfile.flush()

            def flush_pending_downstream_lines() -> None:
                if not pending_downstream_lines:
                    return
                for pending_line in pending_downstream_lines:
                    self.wfile.write(pending_line)
                pending_downstream_lines.clear()
                self.wfile.flush()

            def write_response_failed_event(error_payload: Mapping[str, Any]) -> None:
                pending_downstream_lines.clear()
                error_value = error_payload.get("error")
                response_payload = {
                    "id": f"resp_{uuid.uuid4().hex[:12]}",
                    "object": "response",
                    "status": "failed",
                    "model": model,
                    "output": [],
                    "error": error_value if isinstance(error_value, Mapping) else {"message": str(error_value or "Upstream stream error")},
                }
                self._write_sse_event(
                    "response.failed",
                    {"type": "response.failed", "response": response_payload},
                )

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
                self._write_sse_event("response.completed", event)
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
                            write_proxy_event(
                                "upstream_stream_error_event",
                                request_id=request_id,
                                model=model,
                                upstream=upstream_name,
                                status=502,
                                upstream_format=upstream_format,
                                inbound_format=inbound_format,
                                failure_class=_upstream_failure_class(exc),
                                detail=safe_upstream_error_detail(exc),
                            )
                            write_response_failed_event(usage_payload)
                            _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                            return 502
                        if _responses_events_have_terminal([usage_payload]):
                            saw_terminal_event = True
                        if event_type == "response.completed":
                            saw_completed_event = True
                        if _responses_event_has_visible_or_tool_output(usage_payload, upstream_name):
                            visible_or_tool_output_seen = True
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
                                and (upstream_name == "official" or is_reasoning_done)
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
                    line = compatible_sse_line(line, upstream_name, event_context=event_context)
                    rewritten_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                    if isinstance(rewritten_payload, Mapping):
                        remember_completed_tool_event(rewritten_payload)
                    elif isinstance(usage_payload, Mapping):
                        remember_completed_tool_event(usage_payload)
                    _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payload)

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
                    if saw_terminal_event:
                        break
            except UpstreamStreamIdleTimeoutError as exc:
                if defer_stream_errors and not downstream_output_started:
                    raise
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
                    detail=safe_upstream_error_detail(exc),
                )
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_idle_timeout",
                    detail=safe_upstream_error_detail(exc),
                )
                _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                return 502
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if defer_stream_errors and not downstream_output_started:
                    raise UpstreamStreamInterruptedError(exc) from exc
                self.close_connection = True
                write_proxy_event(
                    "upstream_stream_interrupted",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    error=type(exc).__name__,
                    detail=safe_upstream_error_detail(exc),
                )
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    exc=exc,
                )
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                return 502
            if status < 400 and not saw_terminal_event:
                if synthesize_completed_tool_response():
                    self.close_connection = True
                    _capture_usage(usage_capture, None, missing_reason="synthetic_tool_terminal")
                    return status
                if defer_stream_errors and not downstream_output_started:
                    raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")
                self.close_connection = True
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
                    completed_tool_calls=len(completed_tool_output_items),
                    pending_downstream_lines=len(pending_downstream_lines),
                    pending_downstream_bytes=sum(len(pending_line) for pending_line in pending_downstream_lines),
                    last_event_type=last_response_event_type,
                )
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream Responses stream ended without a terminal event.",
                )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                return 502
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
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_empty_completed_response",
                    detail=detail,
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
                if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
                else "upstream_missing_usage",
            )
            return status

        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True
        _capture_usage(
            usage_capture,
            None,
            missing_reason="async_usage_pending"
            if behavior_profile == BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
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
    logger.info("serving Codex proxy on %s:%s", host, port)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex model routing proxy.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
