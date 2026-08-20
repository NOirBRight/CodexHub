from __future__ import annotations

import sys

from python_runtime_contract import require_python_313

require_python_313(__file__)

# RoutePlan/settings retain late compatibility bindings to the public
# ``codex_proxy`` import surface. When this file is launched as a script,
# publish the running module under that name before those bindings can import a
# second copy (and create a second event writer for the same sink).
if __name__ == "__main__":
    sys.modules.setdefault("codex_proxy", sys.modules[__name__])

import argparse
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
import ssl
from functools import partial
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import tomllib
from typing import Any, Callable, Iterable, Mapping, NoReturn
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

from gateway_catalog_runtime import CatalogFacts, CatalogRuntime
from gateway_exchange import (
    ExchangeFailureTypes,
    ExchangeHooks,
    ExchangeProgress,
    ExchangeRequest,
    InboundRequest,
    InboundRequestHooks,
    OpenExchangeRequest,
    ParsedInboundRequest as GatewayRequestInput,
    RelayExchangeRequest,
    execute_exchange,
    parse_inbound_request,
    terminal_result,
)
from vision_proxy import (
    IMAGE_PROXY_CACHE_LOCK,
    VisionFacts,
    VisionProxyAdapter,
    VisionProxyHooks,
    VisionProxyPolicy,
)
import gateway_transport
from gateway_transport import (
    OFFICIAL_CONNECT_TIMEOUT_SECONDS,
    OFFICIAL_HTTP_POOLS,
    OFFICIAL_HTTP_POOLS_LOCK,
    OFFICIAL_POOL_MAX_CONNECTIONS,
    OFFICIAL_POOL_MAX_IDLE_SECONDS,
    OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS,
    OFFICIAL_TCP_KEEPALIVE_IDLE_MS,
    OFFICIAL_TCP_KEEPALIVE_INTERVAL_MS,
    OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS,
    GatewayTransport,
    TransportFacts,
    UpstreamSseReaderLifecycle,
    _OfficialHTTPSConnection,
    _OfficialHTTPSConnectionPool,
    _OfficialPooledResponse,
    _TRANSPORT_PHASE_ATTRIBUTE,
    _UpstreamSseReaderLifecycle,
    _capacity_retry_elapsed_limit_allows,
    _clamp_timeout_to_pre_response_budget,
    _clear_official_attempt_state,
    _configure_official_windows_keepalive,
    _connection_disposition,
    _explicit_transport_phase,
    _failure_class_from_error_values,
    _get_header,
    _header_items,
    _http_error_body_bytes,
    _http_error_payload,
    _http_error_values,
    _http_error_values_contain,
    _http_retry_header_override,
    _official_attempt_connection_disposition,
    _official_attempt_request_write_deadline,
    _official_socket_options,
    _payload_error_values,
    _propagate_transport_metadata,
    _remaining_pre_response_budget_seconds,
    _require_retry_delay_within_pre_response_budget,
    _reset_official_attempt_state,
    _retry_after_delay_seconds,
    _retry_attempts_for_failure_class,
    _set_official_attempt_connection_disposition,
    _status_allows_capacity_error_value,
    _stdlib_transport_error,
    _upstream_error_retryable,
    _upstream_failure_class,
    _upstream_retry_status,
    bind_transport_failure_types,
    official_pool_manager as _transport_official_pool_manager,
    official_proxy_url as _transport_official_proxy_url,
    official_urlopen as _transport_official_urlopen,
    transport_failure_phase,
)

from sse_events import (
    DEFAULT_MAX_FRAME_BYTES,
    SseAssemblerClosedError,
    SseEvent,
    SseEventAssembler,
    SseFrameTooLargeError,
)
from protocol_translation import (
    ChatToResponsesStreamConverter,
    NonForwardable,
    PreparedExchange,
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
    prepare_exchange,
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
    HostedCapabilityFacts as RuntimeHostedCapabilityFacts,
    ProtocolCapabilities as RuntimeProtocolCapabilities,
    ToolCompatibilityError as RuntimeToolCompatibilityError,
    ToolCompatibilityPlan as RuntimeToolCompatibilityPlan,
    build_tool_compatibility_plan,
)

from codex_semantic_adapter import (
    COLLABORATION_V1 as _COLLABORATION_V1,
    COLLABORATION_V2 as _COLLABORATION_V2,
    COLLABORATION_V2_NAMESPACE as _COLLABORATION_V2_NAMESPACE,
    CollaborationBoundaryError as _CollaborationBoundaryError,
    classify_collaboration_payload as _classify_collaboration_payload,
    coerce_number as _semantic_coerce_number,
    coerce_target as _semantic_coerce_target,
    coerce_targets as _semantic_coerce_targets,
    multi_agent_discovery_arguments as _semantic_multi_agent_discovery_arguments,
    normalize_multi_agent_arguments as _semantic_normalize_multi_agent_arguments,
    normalize_tool_search_arguments as _semantic_normalize_tool_search_arguments,
)
from collaboration_adapter import (
    COLLABORATION_BOUNDARY_ERROR_CODE,
    LEGACY_NATIVE_WORKER_SPAWN_FIELDS,
    LEGACY_NATIVE_WORKER_SPAWN_METADATA_FIELD,
    WORKER_BINDING_ERROR_CODE,
    WORKER_REQUESTED_BINDING_FIELD,
    WORKER_REQUESTED_BINDING_FIELDS,
    WORKER_REQUESTED_BINDING_VERSION,
    WORKER_SELECTOR_ERROR_CODE,
    CollaborationAdapter,
    CollaborationFacts,
    PathBindingSigner,
)
from apply_patch_adapter import (
    APPLY_PATCH_ADAPTER_EVENT,
    APPLY_PATCH_ADAPTER_ERROR_CODE,
    APPLY_PATCH_CUSTOM_TOOL_HISTORY_CALL_FIELDS,
    APPLY_PATCH_CUSTOM_TOOL_HISTORY_NATIVE_FIELDS,
    APPLY_PATCH_CUSTOM_TOOL_HISTORY_OUTPUT_FIELDS,
    APPLY_PATCH_FUNCTION_CALL_FIELDS,
    APPLY_PATCH_HISTORY_ADAPTER_EVENT,
    ApplyPatchAdapter,
    ApplyPatchFacts,
    _ThirdPartyApplyPatchStreamAdapter as _ApplyPatchStreamAdapterImpl,
)
from tool_surface_adapter import (
    APPLY_PATCH_FUNCTION_NAME,
    INTERNAL_INPUT_ITEM_TYPES,
    MULTI_AGENT_DISCOVERY_TOOLS,
    MULTI_AGENT_NAMESPACE_ALIASES,
    NODE_REPL_NAMESPACE,
    THIRD_PARTY_TOOL_NAME_ALIASES,
    TOOL_NAME_RE,
    TOOL_SEARCH_EMPTY_MISS_BOUND,
    TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL,
    TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION,
    TOOL_SEARCH_UNAVAILABLE_STATUS,
    ToolSurfaceAdapter,
    ToolSurfaceFacts,
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
from subagent_scheduler import bounded_workflow_from_exact_prompts, compute_allowed_actions
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

OFFICIAL_PASSTHROUGH_FIRST_EVENT_ATTEMPTS = 2


def _official_proxy_url(url: str) -> str | None:
    return _transport_official_proxy_url(
        url,
        getproxies_fn=getproxies,
        getproxies_registry_fn=getproxies_registry,
        proxy_bypass_fn=proxy_bypass,
        platform=sys.platform,
    )


def _official_pool_manager(url: str) -> Any:
    return _transport_official_pool_manager(
        url,
        pools=OFFICIAL_HTTP_POOLS,
        pools_lock=OFFICIAL_HTTP_POOLS_LOCK,
        proxy_url=_official_proxy_url(url),
    )


def _official_urlopen(request: Request, *, timeout: float) -> Any:
    return _transport_official_urlopen(
        request,
        timeout=timeout,
        pool_manager=_official_pool_manager,
    )


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
MULTI_AGENT_TOOL_NAMES = {
    "spawn_agent",
    "wait_agent",
    "close_agent",
    "resume_agent",
    "send_input",
}
MULTI_AGENT_DISCOVERY_QUERY = "spawn_agent multi_agent subagent native Codex"
TOOL_PROTOCOLS = {"auto", "responses_structured", "chat_tools", "text_compat", "none"}
STRUCTURED_TOOL_PROTOCOLS = {"responses_structured", "chat_tools"}
TOOL_SURFACE_STRATEGIES = {"eager", "deferred_core"}
NATIVE_RESPONSES_TOOL_CODECS = {"none", "strict_apply_patch"}
NATIVE_RESPONSES_TOOL_CONTRACT_ERROR_CODE = "invalid_native_responses_tool_contract"
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
from route_primitives import (
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    DEFAULT_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS,
    DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS,
    DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS,
    DEFAULT_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS,
    DEFAULT_STREAM_RETRY_ELAPSED_LIMIT_SECONDS,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    RETRY_REQUEST_MAIN_GENERATION,
    RETRY_REQUEST_COMPACT,
    RETRY_REQUEST_IMAGE_PROXY_VISION,
    RETRY_REQUEST_OFFICIAL_CONTROL,
    BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
    BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
    BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
    BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
    WIRE_TRANSPARENT,
    WIRE_RESPONSES_TO_CHAT,
    WIRE_CHAT_TO_RESPONSES,
    CODEX_SEMANTIC_EXTERNAL_ADAPTER,
    CODEX_SEMANTIC_NONE,
    REQUEST_KIND_GATEWAY,
    REQUEST_KIND_TRANSPARENT,
    RETRY_GATEWAY_FULL,
    RETRY_CONSERVATIVE_PRE_OUTPUT,
    USAGE_SYNC_CAPTURE,
    USAGE_ASYNC_TAP,
    REPAIR_CODEX_SUBAGENT,
    REPAIR_NONE,
    VISION_PROXY_DISABLED,
    VISION_PROXY_CODEX_APP_ADAPTER,
    VISION_PROXY_TRANSPARENT_OVERLAY,
    ROUTE_PLAN_SCHEMA_VERSION,
    CapabilityState,
    RouteProtocol,
    AttemptRequestBodyMode,
    CallerRequestBodyMode,
    AuthenticationStrategy,
    SensitiveValue,
    OperationalAuthentication,
    FrozenRequestHeaders,
    ToolExposureMode,
    VisionAction,
    VisionNetworkAction,
    CollaborationBackend,
    CodexCompatibilityPolicy,
    ExecutionOwner,
    StreamingPolicy,
    RetryPolicy,
    UsagePolicy,
    TransportPolicy,
    MutationPolicy,
    RouteMutation,
    RETRY_FAILURE_QUICK_TRANSIENT,
    RETRY_FAILURE_PROVIDER_THROTTLE,
    RETRY_FAILURE_PROVIDER_OVERLOADED,
    RETRY_FAILURE_PERMANENT,
    CAPACITY_RETRY_FAILURE_CLASSES,
    CAPACITY_RETRY_CADENCE_SECONDS,
    TRANSIENT_HTTP_RETRY_STATUSES,
    AUTO_UPSTREAM_PROTOCOL_FALLBACK_STATUSES,
    RETRY_SAFETY_SAFE_PREWRITE,
    RETRY_SAFETY_GUARANTEED_IDEMPOTENT,
    RETRY_SAFETY_SUPPRESSED_POST_WRITE,
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
    RETRY_SAFETY_UNKNOWN,
    PERMANENT_HTTP_ERROR_STATUSES,
    PERMANENT_UPSTREAM_ERROR_VALUES,
    PERMANENT_UPSTREAM_ERROR_NEEDLES,
    PERMANENT_UPSTREAM_AUTH_NEEDLES,
    PROVIDER_THROTTLE_ERROR_VALUES,
    PROVIDER_THROTTLE_ERROR_NEEDLES,
    PROVIDER_OVERLOADED_ERROR_VALUES,
    PROVIDER_OVERLOADED_ERROR_NEEDLES,
    IMAGE_PROXY_PROMPT_VERSION,
    IMAGE_PROXY_PROMPT,
    IMAGE_PROXY_PROGRESS_TEXT,
)

from gateway_errors import (
    ImageProxyError,
    ModelIdentityResolutionError,
    UnsupportedRouteProtocolError,
    UnqualifiedRouteProtocolError,
    CompactEmptyResponseError,
    LifecycleEmptyFinalResponseError,
    LifecycleFinalFormatResponseError,
    UpstreamProtocolTranslationError,
    UpstreamStreamIdleTimeoutError,
    GatewayPreResponseBudgetExhausted,
    _catalog_failure,
    _identity_failure,
)

logger = logging.getLogger("codex_proxy")
UpstreamSseReaderLifecycle.default_logger_provider = lambda: logger
IMAGE_PROXY_CACHE_PATH = RUNTIME_PROXY_DIR / "image-proxy-cache.sqlite"




from gateway_settings import (
    _runtime_proxy_dir,
    upstream_timeout_seconds,
    sse_keepalive_seconds,
    _number_setting_or_env,
    transport_sse_idle_timeout_seconds,
    model_event_sse_idle_timeout_seconds,
    pre_output_sse_idle_timeout_seconds,
    post_content_sse_idle_timeout_seconds,
    official_upstream_open_attempts,
    _env_flag,
    _runtime_settings_value,
    _request_kind_retry_env_name,
    _request_kind_retry_settings_name,
    _default_retry_attempts_for_request_kind,
    _bounded_retry_attempts,
    _upstream_retry_attempts,
    _request_kind_retry_attempts_configured,
    gateway_client_key,
    max_request_body_bytes,
    _env_or_settings_flag,
    gateway_auto_retry_enabled,
    gateway_official_http_passthrough_enabled,
    gateway_websocket_recorder_enabled,
    gateway_websocket_recorder_max_frames,
    gateway_websocket_recorder_idle_timeout_seconds,
    gateway_auto_retry_max_attempts,
    gateway_capacity_retry_elapsed_limit_seconds,
    gateway_stream_retry_elapsed_limit_seconds,
    gateway_downstream_retry_notice_enabled,
    gateway_capacity_retry_delay_seconds,
    subagent_assist_mode,
    subagent_guidance_enabled,
    subagent_semantic_repair_enabled,
    lifecycle_empty_final_resample_enabled,
    gateway_retry_delay_seconds,
    gateway_image_proxy_enabled,
    gateway_image_proxy_model,
)



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


def _collaboration_adapter() -> CollaborationAdapter:
    """Build a request-time adapter so emit and signing-root patches stay live."""
    return CollaborationAdapter(
        facts=CollaborationFacts(signing_root=WORKER_BINDING_SIGNING_ROOT),
        emit=write_proxy_event,
        signer=PathBindingSigner(WORKER_BINDING_SIGNING_ROOT),
    )


def _tool_surface_adapter() -> ToolSurfaceAdapter:
    """Build a request-time adapter so apply-patch and message patches stay live."""
    return ToolSurfaceAdapter(
        facts=ToolSurfaceFacts(),
        adapt_apply_patch_history=_adapt_apply_patch_custom_tool_history,
        compatible_internal_message=_compatible_internal_message,
        transcript_message=_assistant_transcript_message,
    )


def _apply_patch_adapter() -> ApplyPatchAdapter:
    """Build a request-time adapter so event-writer and terminal-type patches stay live."""
    return ApplyPatchAdapter(
        facts=ApplyPatchFacts(terminal_event_types=frozenset(RESPONSES_TERMINAL_EVENT_TYPES)),
        write_event=_write_adapter_event,
    )


def _vision_proxy_adapter() -> VisionProxyAdapter:
    """Build a request-time Vision Proxy seam so facade patches stay live."""

    return VisionProxyAdapter(
        facts=VisionFacts(
            cache_path=Path(IMAGE_PROXY_CACHE_PATH),
            prompt_version=IMAGE_PROXY_PROMPT_VERSION,
            prompt=IMAGE_PROXY_PROMPT,
            cache_lock=IMAGE_PROXY_CACHE_LOCK,
            downstream_closed_error=DownstreamClosedDuringImageProxyError,
        ),
        hooks=VisionProxyHooks(
            enabled_reader=gateway_image_proxy_enabled,
            vision_model_reader=gateway_image_proxy_model,
            resolve_upstream=choose_upstream,
            model_supports_image=model_supports_image,
            canonical_model_id=canonical_model_id,
            compatible_request_body=compatible_request_body,
            strip_tools=_strip_tools_for_text_only_proxy_payload,
            responses_url=_responses_url,
            chat_completions_url=_chat_completions_url,
            prepare_exchange=prepare_exchange,
            upstream_headers=upstream_headers,
            open_upstream=_open_upstream_response,
            upstream_timeout_seconds=upstream_timeout_seconds,
            response_is_event_stream=_is_event_stream,
            events_to_responses_body=_events_to_responses_body,
            write_event=_write_adapter_event,
            usage_from_payload=_usage_from_payload,
            normalize_usage=_normalize_usage_for_event,
            safe_upstream_error_detail=safe_upstream_error_detail,
            cache_lookup_override=_vision_proxy_override(_image_proxy_cache_lookup, _VISION_ORIGINAL_CACHE_LOOKUP),
            cache_store_override=_vision_proxy_override(_image_proxy_cache_store, _VISION_ORIGINAL_CACHE_STORE),
            response_body_override=_vision_proxy_override(_image_proxy_response_body, _VISION_ORIGINAL_RESPONSE_BODY),
            response_text_override=_vision_proxy_override(_extract_model_response_text, _VISION_ORIGINAL_EXTRACT_TEXT),
            describe_image_override=_vision_proxy_override(_call_vision_model_for_image_description, _VISION_ORIGINAL_DESCRIBE_IMAGE),
            description_for_part_override=_vision_proxy_override(_image_proxy_description_for_part, _VISION_ORIGINAL_DESCRIPTION_FOR_PART),
            vision_upstream_override=_vision_proxy_override(_image_proxy_vision_upstream, _VISION_ORIGINAL_UPSTREAM),
            apply_responses_override=_vision_proxy_override(apply_image_proxy_to_responses_payload, _VISION_ORIGINAL_APPLY_RESPONSES),
            apply_chat_override=_vision_proxy_override(apply_image_proxy_to_chat_payload, _VISION_ORIGINAL_APPLY_CHAT),
            sse_assembler_factory=SseEventAssembler,
            response_event_payload=_converted_sse_payload,
            boundary_override=_vision_proxy_override(apply_vision_proxy_adapter, _VISION_ORIGINAL_BOUNDARY),
        ),
    )


def _raise_collaboration_boundary_error(
    event_context: Mapping[str, Any] | None,
    *,
    classification: str,
    message: str,
    surface: str = "request",
    cause: BaseException | None = None,
) -> NoReturn:
    _collaboration_adapter().raise_boundary_error(
        event_context,
        classification=classification,
        message=message,
        surface=surface,
        cause=cause,
    )


def _resolve_collaboration_boundary(
    payload: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str = "request",
) -> str | None:
    return _collaboration_adapter().resolve_boundary(
        payload,
        event_context,
        surface=surface,
    )


def _is_collaboration_v2_context(event_context: Mapping[str, Any] | None) -> bool:
    return _collaboration_adapter().is_v2_context(event_context)


def _collaboration_context_with_protocol(
    event_context: Mapping[str, Any] | None,
    protocol: str | None,
) -> Mapping[str, Any] | None:
    return _collaboration_adapter().context_with_protocol(event_context, protocol)


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


def _catalog_runtime() -> CatalogRuntime:
    """Build a request-time catalog seam so facade monkeypatches stay live."""

    return CatalogRuntime(
        facts=CatalogFacts(
            generated_catalog_path=GENERATED_CATALOG_PATH,
            legacy_generated_catalog_path=LEGACY_GENERATED_CATALOG_PATH,
            policy_path=POLICY_PATH,
            official_base_url=OFFICIAL_BASE_URL,
            ollama_cloud_base_url=OLLAMA_CLOUD_BASE_URL,
            default_official_prefixes=tuple(DEFAULT_OFFICIAL_PREFIXES),
            official_alias_prefix=OFFICIAL_ALIAS_PREFIX,
            ollama_cloud_alias_prefix=OLLAMA_CLOUD_ALIAS_PREFIX,
            official_fast_variant_service_tier=OFFICIAL_FAST_VARIANT_SERVICE_TIER,
            official_fast_variant_base_models=dict(OFFICIAL_FAST_VARIANT_BASE_MODELS),
            official_fast_variant_display_names=dict(OFFICIAL_FAST_VARIANT_DISPLAY_NAMES),
            upstream_max_output_token_caps=dict(UPSTREAM_MAX_OUTPUT_TOKEN_CAPS),
            official_refresh_state_filename=OFFICIAL_REFRESH_STATE_FILENAME,
        ),
        catalog_path_reader=existing_generated_catalog_path,
        catalog_models_reader=load_catalog_models,
        policy_reader=load_policy,
        routing_config_reader=lambda: load_routing_config(),
        external_model_reader=resolve_external_model_alias,
        ollama_model_reader=resolve_ollama_cloud_model,
        vision_proxy_enabled_reader=gateway_image_proxy_enabled,
        official_base_url_reader=_catalog_override(official_base_url, _CATALOG_ORIGINAL_OFFICIAL_BASE_URL),
        ollama_base_url_reader=_catalog_override(ollama_cloud_base_url, _CATALOG_ORIGINAL_OLLAMA_BASE_URL),
        official_fast_projection_reader=_catalog_override(catalog_with_official_fast_variants, _CATALOG_ORIGINAL_FAST_VARIANTS),
        context_guard_reader=_catalog_override(catalog_with_openai_context_guard, _CATALOG_ORIGINAL_CONTEXT_GUARD),
        vision_projection_reader=_catalog_override(catalog_with_vision_proxy_capabilities, _CATALOG_ORIGINAL_VISION_PROJECTION),
        canonical_models_reader=_catalog_override(canonical_catalog_models, _CATALOG_ORIGINAL_CANONICAL_MODELS),
        modalities_reader=_catalog_override(_modalities_include_image, _CATALOG_ORIGINAL_MODALITIES),
        input_modalities_reader=_catalog_override(_catalog_input_modalities, _CATALOG_ORIGINAL_INPUT_MODALITIES),
        generated_catalog_by_slug_reader=_catalog_override(generated_catalog_by_slug, _CATALOG_ORIGINAL_BY_SLUG),
        published_budget_reader=_catalog_override(published_official_context_budgets, _CATALOG_ORIGINAL_PUBLISHED_BUDGETS),
        known_official_ids_reader=catalog_known_official_model_ids,
        official_display_name_reader=official_short_display_name,
        catalog_by_slug_reader=lambda: generated_catalog_by_slug(),
        published_model_reader=_published_catalog_model,
        generated_official_reader=generated_official_catalog_upstream_model,
        official_alias_reader=official_alias_upstream_model,
        official_fast_variant_reader=official_fast_variant_upstream_model,
        ollama_runtime_reader=ollama_cloud_runtime_upstream,
        ollama_alias_reader=ollama_cloud_alias_upstream_model,
        should_include_model_reader=should_include_model,
        should_include_external_model_reader=should_include_external_provider_model,
        model_visibility_reader=model_visibility,
        internal_model_reader=is_internal_model,
    )


def official_prefixes() -> tuple[str, ...]:
    return _catalog_runtime().official_prefixes()


def official_base_url() -> str:
    return _catalog_runtime().official_base_url()


def ollama_cloud_base_url() -> str:
    return _catalog_runtime().ollama_cloud_base_url()


def generated_catalog_slugs(path: Path = GENERATED_CATALOG_PATH) -> set[str]:
    return _catalog_runtime().generated_catalog_slugs(path)


def generated_catalog_by_slug(path: Path = GENERATED_CATALOG_PATH) -> dict[str, dict[str, Any]]:
    return _catalog_runtime().generated_catalog_by_slug(path)


def _catalog_identity_slug(slug: str) -> str:
    return _catalog_runtime().catalog_identity_slug(slug)


def _published_catalog_model(slug: str) -> dict[str, Any] | None:
    return _catalog_runtime().published_catalog_model(slug)


def _is_internal_route_identity(value: Any) -> bool:
    return _catalog_runtime().is_internal_route_identity(value)


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
    _catalog_runtime().validate_published_model_for_provider(
        model,
        provider_id=provider_id,
        model_slug=model_slug,
        expected_upstream_model=expected_upstream_model,
    )


def _provider_catalog_failure(
    message: str,
    *,
    provider_id: str,
    model_slug: str,
) -> ModelIdentityResolutionError:
    return CatalogRuntime.provider_catalog_failure(
        message,
        provider_id=provider_id,
        model_slug=model_slug,
    )


def _resolve_external_model_alias(slug: str) -> dict[str, Any] | None:
    return _catalog_runtime().resolve_external_model(slug)


def _resolve_ollama_cloud_model(
    model_id: str,
    *,
    require_api_key: bool,
) -> tuple[bool, dict[str, Any] | None]:
    return _catalog_runtime().resolve_ollama_cloud_model(
        model_id,
        require_api_key=require_api_key,
    )


def _catalog_output_limit(model_id: str) -> tuple[int | None, bool]:
    return _catalog_runtime().catalog_output_limit(model_id)


def catalog_max_output_tokens(model_id: str) -> int | None:
    return _catalog_runtime().catalog_max_output_tokens(model_id)


def policy_denies_model(model_id: Any, policy: Any) -> bool:
    return CatalogRuntime.policy_denies_model(model_id, policy)


def policy_denies_any_model(model_ids: tuple[Any, ...], policy: Any) -> bool:
    return _catalog_runtime().policy_denies_any_model(model_ids, policy)


def generated_official_catalog_upstream_model(slug: str, policy: Any) -> str | None:
    return _catalog_runtime().generated_official_upstream_model(slug, policy)


def official_alias_upstream_model(slug: str, policy: Any) -> str | None:
    return _catalog_runtime().official_alias_upstream_model(slug, policy)


def official_fast_variant_upstream_model(slug: str, policy: Any) -> str | None:
    return _catalog_runtime().official_fast_variant_upstream_model(slug, policy)


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
    return provider or None


def provider_scoped_route_model(model_id: str | None, provider_hint: str | None) -> str | None:
    if not model_id:
        return None
    slug = canonical_model_id(str(model_id))
    if not slug or not provider_hint:
        return slug
    provider = canonical_model_id(str(provider_hint))
    if not provider or slug.startswith(f"{provider}/"):
        return slug
    return f"{provider}/{slug}"


def ollama_cloud_runtime_upstream(model_id: str, policy: Any) -> dict[str, Any] | None:
    return _catalog_runtime().ollama_cloud_runtime_upstream(model_id, policy)


def ollama_cloud_alias_upstream_model(slug: str, policy: Any) -> dict[str, Any] | None:
    return _catalog_runtime().ollama_cloud_alias_upstream_model(slug, policy)


def _route_capability_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    return CatalogRuntime.route_capability_metadata(source)


def choose_upstream(model_id: str) -> dict[str, Any]:
    return _catalog_runtime().choose_upstream(model_id)


def official_upstream() -> dict[str, Any]:
    runtime = _catalog_runtime()
    return {
        "name": "official",
        "provider_id": "openai",
        "base_url": runtime.official_base_url(),
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



from gateway_relay import (
    RelayContext,
    RelaySymbols,
    SseLineRelayContext,
    iter_upstream_sse_lines,
    relay_raw_response,
    relay_upstream_response,
    send_sse_headers,
    write_non_streaming_body,
    write_sse_bytes,
    write_sse_data,
    write_sse_done,
    write_sse_event,
    write_sse_keepalive,
)
from gateway_sse import (
    DownstreamStreamCommit,
    PassthroughSseSemanticStats,
    _sse_line_ending,
    _sse_event_separator_after_line,
    _is_sse_blank_line,
    _is_sse_event_metadata_line,
    _sse_payload_bytes,
    _parse_sse_json_payload,
    _parse_sse_json_payloads,
)

_GatewayDownstreamStreamCommit = DownstreamStreamCommit

class UpstreamSseSemanticError(ValueError):
    """A complete converted SSE frame is not valid source-protocol JSON."""

    def __init__(
        self,
        message: str,
        *,
        classification: str = "upstream_protocol_error",
    ) -> None:
        self.classification = classification
        super().__init__(message)


class _RuntimeToolInverseStreamError(ValueError):
    """A runtime-tool inverse failed after converted streaming began."""

    def __init__(self, translation_error: UpstreamProtocolTranslationError) -> None:
        self.translation_error = translation_error
        super().__init__(str(translation_error))


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
    turn_metadata = _get_header(headers, "x-codex-turn-metadata")
    if isinstance(turn_metadata, str):
        try:
            parsed_turn_metadata = json.loads(turn_metadata)
        except json.JSONDecodeError:
            parsed_turn_metadata = None
        if (
            isinstance(parsed_turn_metadata, Mapping)
            and parsed_turn_metadata.get("request_kind") == "compaction"
        ):
            return RETRY_REQUEST_COMPACT
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


bind_transport_failure_types(
    stream_interrupted_error=UpstreamStreamInterruptedError,
    stream_error_event=UpstreamStreamErrorEvent,
)


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
    return _tool_surface_adapter().valid_tool_name(value)


def _is_tool_call_item(item: Mapping[str, Any]) -> bool:
    return _tool_surface_adapter().is_tool_call_item(item)


def _has_invalid_tool_name(item: Mapping[str, Any]) -> bool:
    return _tool_surface_adapter().has_invalid_tool_name(item)


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
    return _tool_surface_adapter().tool_schema_name(value)


def _tool_parameters_schema(value: Mapping[str, Any]) -> dict[str, Any]:
    return _tool_surface_adapter().tool_parameters_schema(value)


def _explicit_function_tool(name: str, description: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    return _tool_surface_adapter().explicit_function_tool(name, description, parameters)


def _multi_agent_explicit_function_tools(
    include_spawn_agent: bool = True,
    include_wait_agent: bool = True,
    include_close_agent: bool = True,
    include_resume_agent: bool = True,
    include_send_input: bool = True,
    open_agent_ids: list[str] | None = None,
    wait_agent_ids: list[str] | None = None,
    close_agent_ids: list[str] | None = None,
    worker_selector_values: tuple[str, ...] = ("worker", "default"),
) -> list[dict[str, Any]]:
    return _tool_surface_adapter().multi_agent_explicit_function_tools(
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


def _supports_explicit_namespace_alias(namespace_name: str) -> bool:
    return _tool_surface_adapter().supports_explicit_namespace_alias(namespace_name)


def _is_multi_agent_namespace_name(name: str | None) -> bool:
    return _tool_surface_adapter().is_multi_agent_namespace_name(name)


def _is_multi_agent_explicit_tool_name(name: str) -> bool:
    return _tool_surface_adapter().is_multi_agent_explicit_tool_name(name)


def _multi_agent_alias_tool_name(name: Any) -> str | None:
    return _tool_surface_adapter().multi_agent_alias_tool_name(name)


def _looks_like_response_tool_name_fragment(value: Mapping[str, Any]) -> bool:
    return _tool_surface_adapter().looks_like_response_tool_name_fragment(value)


def _is_multi_agent_tool_schema(value: Any) -> bool:
    return _tool_surface_adapter().is_multi_agent_tool_schema(value)


def _is_node_repl_explicit_tool_name(name: str) -> bool:
    return _tool_surface_adapter().is_node_repl_explicit_tool_name(name)


def _is_node_repl_tool_schema(value: Any) -> bool:
    return _tool_surface_adapter().is_node_repl_tool_schema(value)


def _is_local_tool_gateway_tool_schema(value: Any) -> bool:
    return _tool_surface_adapter().is_local_tool_gateway_tool_schema(value)


def _is_mcp_or_codex_app_tool_schema(value: Any) -> bool:
    return _tool_surface_adapter().is_mcp_or_codex_app_tool_schema(value)


def _is_flattened_namespace_schema(value: Any) -> bool:
    return _tool_surface_adapter().is_flattened_namespace_schema(value)


def _is_raw_namespace_schema(value: Any) -> bool:
    return _tool_surface_adapter().is_raw_namespace_schema(value)


def _valid_namespace_function_names(value: Any) -> tuple[str, tuple[str, ...]] | None:
    return _tool_surface_adapter().valid_namespace_function_names(value)


def _deferred_namespace_surface_counts(
    source_tools: list[Any],
    final_tools: list[Any],
) -> tuple[int, int]:
    return _tool_surface_adapter().deferred_namespace_surface_counts(source_tools, final_tools)


def _flatten_namespace_function_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return _tool_surface_adapter().flatten_namespace_function_tools(tools)


def _multi_agent_function_call_name(item: Mapping[str, Any]) -> str | None:
    return _tool_surface_adapter().multi_agent_function_call_name(item)


def _node_repl_function_call_name(item: Mapping[str, Any]) -> str | None:
    return _tool_surface_adapter().node_repl_function_call_name(item)


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
        UnsupportedProtocolTranslationError(error.code, str(error)),
        classification=error.classification,
    ) from error


def _runtime_tool_alias_token(
    declarations: list[Any],
    *,
    selected_protocol: str,
    protocol_capabilities: RuntimeProtocolCapabilities,
    provider_hosted_capabilities: Any,
) -> str:
    capability_set = {
        "function_lifecycle": protocol_capabilities.function_lifecycle,
        "namespace_lifecycle": protocol_capabilities.namespace_lifecycle,
        "custom_lifecycle": protocol_capabilities.custom_lifecycle,
        "tool_search_lifecycle": protocol_capabilities.tool_search_lifecycle,
        "hosted_lifecycles": sorted(protocol_capabilities.hosted_lifecycles),
        "unknown_lifecycles": sorted(protocol_capabilities.unknown_lifecycles),
        "accepts_namespace_adapter": protocol_capabilities.accepts_namespace_adapter,
        "accepts_custom_adapter": protocol_capabilities.accepts_custom_adapter,
        "accepts_tool_search_adapter": protocol_capabilities.accepts_tool_search_adapter,
        "max_tool_name_length": protocol_capabilities.max_tool_name_length,
        "provider_hosted_kinds": sorted(
            RuntimeHostedCapabilityFacts.from_value(
                provider_hosted_capabilities
            ).supported_kinds
        ),
    }
    canonical = json.dumps(
        {
            "capability_set": capability_set,
            "declarations": declarations,
            "selected_protocol": selected_protocol,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _prepare_runtime_tool_compatibility(
    payload: dict[str, Any],
    upstream: Mapping[str, Any],
    tool_protocol: str,
    event_context: dict[str, Any],
    native_responses_tool_codec: str | None = None,
) -> bool:
    tools = payload.get("tools")
    declarations = tools if isinstance(tools, list) else []
    configured_codec = (
        native_responses_tool_codec
        if native_responses_tool_codec is not None
        else _external_native_responses_tool_codec(upstream)
    )
    # A native Responses codec is not a declaration filter for Chat.  When a
    # maintained model is reachable over both protocols, the Chat compatibility
    # plan must still see the custom declaration and apply its reversible
    # function adapter instead of leaking the Responses-only setting (#285).
    codec = configured_codec if tool_protocol == "responses_structured" else "none"
    if codec == "strict_apply_patch":
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
        provider_hosted_capabilities = upstream.get("hosted_tool_capabilities")
        protocol_capabilities = _runtime_tool_protocol_capabilities(tool_protocol, upstream)
        plan = build_tool_compatibility_plan(
            planned_declarations,
            selected_protocol=tool_protocol,
            provider_hosted_capabilities=provider_hosted_capabilities,
            tool_choice=payload.get("tool_choice"),
            protocol_capabilities=protocol_capabilities,
            request_token=_runtime_tool_alias_token(
                planned_declarations,
                selected_protocol=tool_protocol,
                protocol_capabilities=protocol_capabilities,
                provider_hosted_capabilities=provider_hosted_capabilities,
            ),
            collaboration_protocol=event_context.get("collaboration_protocol"),
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


def _runtime_tool_adapter_alias_hash(aliases: Iterable[str]) -> str:
    """Hash the ordered generated alias surface without logging tool names."""

    encoded = json.dumps(
        list(aliases),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_tool_adapter_request_snapshot(
    plan: RuntimeToolCompatibilityPlan,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return bounded evidence for the body sent to the selected upstream.

    This is intentionally computed after ``encode_payload``.  The capture
    proxy used by the private E2E runner sits before this boundary and can
    therefore only prove the CLI's native collaboration surface; these
    fields are the Gateway's own proof of the final provider wire shape.
    """

    tools = payload.get("tools")
    tool_values = tools if isinstance(tools, list) else []
    aliases = [
        tool.get("name")
        for tool in tool_values
        if isinstance(tool, Mapping)
        and tool.get("type") == "function"
        and isinstance(tool.get("name"), str)
        and plan.registry.record_for_alias(tool.get("name")) is not None
    ]
    namespace_count = sum(
        1
        for tool in tool_values
        if isinstance(tool, Mapping) and tool.get("type") == "namespace"
    )
    namespace_child_count = sum(
        len(tool.get("tools"))
        for tool in tool_values
        if isinstance(tool, Mapping)
        and tool.get("type") == "namespace"
        and isinstance(tool.get("tools"), list)
    )
    history_call_ids: set[str] = set()
    history_output_ids: set[str] = set()
    alias_call_ids: set[str] = set()
    history_call_count = 0
    history_output_count = 0
    input_items = payload.get("input")
    for item in input_items if isinstance(input_items, list) else ():
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call" and plan.registry.record_for_alias(item.get("name")) is not None:
            history_call_count += 1
            if isinstance(call_id, str) and call_id:
                history_call_ids.add(call_id)
                alias_call_ids.add(call_id)
        elif item_type == "function_call_output":
            record = plan.registry.record_for_call(call_id)
            if record is not None or (isinstance(call_id, str) and call_id in alias_call_ids):
                history_output_count += 1
                if isinstance(call_id, str) and call_id:
                    history_output_ids.add(call_id)
    return {
        "adapted_alias_count": len(aliases),
        "adapted_alias_unique_count": len(set(aliases)),
        "adapted_alias_hash": _runtime_tool_adapter_alias_hash(aliases),
        "upstream_function_tool_count": sum(
            1
            for tool in tool_values
            if isinstance(tool, Mapping) and tool.get("type") == "function"
        ),
        "upstream_namespace_count": namespace_count,
        "upstream_namespace_child_count": namespace_child_count,
        "adapted_history_call_count": history_call_count,
        "adapted_history_output_count": history_output_count,
        "adapted_history_pair_count": len(history_call_ids & history_output_ids),
    }


def _runtime_tool_adapter_item_snapshot(
    plan: RuntimeToolCompatibilityPlan,
    value: Any,
) -> dict[str, Any]:
    """Count alias-owned response items without retaining their contents."""

    items = value if isinstance(value, list) else [value]
    call_count = 0
    output_count = 0
    alias_call_ids: set[str] = set()
    aliases: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        candidates = [item]
        nested = item.get("item")
        if isinstance(nested, Mapping):
            candidates.append(nested)
        for candidate in candidates:
            if candidate.get("type") == "function_call":
                name = candidate.get("name")
                if plan.registry.record_for_alias(name) is not None:
                    call_count += 1
                    call_id = candidate.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        alias_call_ids.add(call_id)
                    if isinstance(name, str):
                        aliases.append(name)
            elif candidate.get("type") == "function_call_output":
                call_id = candidate.get("call_id")
                if (
                    plan.registry.record_for_call(call_id) is not None
                    or (isinstance(call_id, str) and call_id in alias_call_ids)
                ):
                    output_count += 1
    return {
        "wire_alias_call_count": call_count,
        "wire_alias_output_count": output_count,
        "wire_alias_hash": _runtime_tool_adapter_alias_hash(aliases),
    }


def _write_runtime_tool_adapter_request_evidence(
    plan: RuntimeToolCompatibilityPlan,
    payload: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> None:
    snapshot = _runtime_tool_adapter_request_snapshot(plan, payload)
    if not plan.has_adaptations and not (
        snapshot["adapted_history_call_count"]
        or snapshot["adapted_history_output_count"]
    ):
        return
    _write_adapter_event(
        event_context,
        "runtime_tool_adapter_request",
        surface="request",
        outcome="adapted",
        **snapshot,
    )


def _write_runtime_tool_adapter_response_evidence(
    plan: RuntimeToolCompatibilityPlan,
    wire_value: Any,
    decoded_value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    snapshot = _runtime_tool_adapter_item_snapshot(plan, wire_value)
    wire_count = snapshot["wire_alias_call_count"] + snapshot["wire_alias_output_count"]
    if not wire_count or wire_value == decoded_value:
        return
    _write_adapter_event(
        event_context,
        "runtime_tool_adapter_response",
        surface=surface,
        outcome="inverse_mapped",
        adapted_alias_hash=_runtime_tool_adapter_alias_hash(plan.aliases),
        reverse_mapping_count=wire_count,
        reverse_mapped_call_count=snapshot["wire_alias_call_count"],
        reverse_mapped_output_count=snapshot["wire_alias_output_count"],
        **snapshot,
    )


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
    return _tool_surface_adapter().runtime_plan_has_native_plain_function(plan, item)


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
    return _tool_surface_adapter().structured_tool_function_call_item(item)


def _same_selected_v1_collaboration_function_call(
    item: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> bool:
    return _tool_surface_adapter().same_selected_v1_collaboration_function_call(item, event_context)


def _hoist_additional_tools_input_items(payload: dict[str, Any]) -> bool:
    return _tool_surface_adapter().hoist_additional_tools_input_items(payload)


def _rewrite_structured_tool_input_items(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
    compatibility_plan: RuntimeToolCompatibilityPlan | None = None,
) -> bool:
    changed = _tool_surface_adapter().rewrite_structured_tool_input_items(
        payload,
        event_context=event_context,
        compatibility_plan=compatibility_plan,
    )
    if changed:
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
    deferred_core_surface: bool = False,
    tool_surface_counts: dict[str, int] | None = None,
    tool_surface_source_tools: list[Any] | None = None,
    open_agent_ids: list[str] | None = None,
    wait_agent_ids: list[str] | None = None,
    close_agent_ids: list[str] | None = None,
    worker_selector_values: tuple[str, ...] = ("worker", "default"),
) -> bool:
    return _tool_surface_adapter().inject_explicit_codex_tools(
        payload,
        include_tool_search=include_tool_search,
        include_multi_agent_tools=include_multi_agent_tools,
        include_spawn_agent=include_spawn_agent,
        include_wait_agent=include_wait_agent,
        include_close_agent=include_close_agent,
        include_resume_agent=include_resume_agent,
        include_send_input=include_send_input,
        include_node_repl_tools=include_node_repl_tools,
        include_local_tool_gateway_tools=include_local_tool_gateway_tools,
        strip_namespace_tools=strip_namespace_tools,
        strip_all_namespace_tools=strip_all_namespace_tools,
        include_flattened_namespace_tools=include_flattened_namespace_tools,
        deferred_core_surface=deferred_core_surface,
        tool_surface_counts=tool_surface_counts,
        tool_surface_source_tools=tool_surface_source_tools,
        open_agent_ids=open_agent_ids,
        wait_agent_ids=wait_agent_ids,
        close_agent_ids=close_agent_ids,
        worker_selector_values=worker_selector_values,
    )


def _restore_deferred_core_node_repl_namespace(
    payload: dict[str, Any],
    source_tools: list[Any] | None,
) -> bool:
    return _tool_surface_adapter().restore_deferred_core_node_repl_namespace(payload, source_tools)


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
    return _tool_surface_adapter().function_tool_names(value)


def _codex_apps_flat_alias_parts(name: Any) -> tuple[str, str] | None:
    return _tool_surface_adapter().codex_apps_flat_alias_parts(name)


def _codex_apps_flat_alias_name(name: Any) -> str | None:
    return _tool_surface_adapter().codex_apps_flat_alias_name(name)


def _split_namespace_tool_alias(name: Any) -> tuple[str, str] | None:
    return _tool_surface_adapter().split_namespace_tool_alias(name)


def _codex_apps_namespace_flat_alias(namespace: Any, name: Any) -> str | None:
    return _tool_surface_adapter().codex_apps_namespace_flat_alias(namespace, name)


def _normalize_tool_search_arguments(value: Any) -> dict[str, Any] | None:
    return _semantic_normalize_tool_search_arguments(value)


def _bounded_empty_tool_search_terminal_calls(value: Any) -> dict[str, tuple[str, int]]:
    return _tool_surface_adapter().bounded_empty_tool_search_terminal_calls(value)


def _terminalize_bounded_empty_tool_search_misses(
    payload: dict[str, Any],
    terminal_calls: Mapping[str, tuple[str, int]],
) -> bool:
    return _tool_surface_adapter().terminalize_bounded_empty_tool_search_misses(payload, terminal_calls)


def _restrict_bounded_tool_search_queries(payload: dict[str, Any], bounded_queries: set[str]) -> bool:
    return _tool_surface_adapter().restrict_bounded_tool_search_queries(payload, bounded_queries)


def _tool_search_query_digest(query: str) -> bytes:
    return _tool_surface_adapter().tool_search_query_digest(query)


def _bounded_tool_search_query_digests(event_context: Mapping[str, Any] | None) -> set[bytes]:
    return _tool_surface_adapter().bounded_tool_search_query_digests(event_context)


def _tool_search_call_arguments(
    value: Mapping[str, Any],
    *,
    candidate_item_ids: set[str] | None = None,
    allow_legacy_function: bool = False,
) -> dict[str, Any] | None:
    return _tool_surface_adapter().tool_search_call_arguments(
        value,
        candidate_item_ids=candidate_item_ids,
        allow_legacy_function=allow_legacy_function,
    )


def _bounded_tool_search_unavailable_message(item: Mapping[str, Any]) -> dict[str, Any]:
    return _tool_surface_adapter().bounded_tool_search_unavailable_message(item)


def _suppress_bounded_tool_search_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    return _tool_surface_adapter().suppress_bounded_tool_search_calls(value, event_context)


def _suppress_bounded_tool_search_calls_inner(
    value: Any,
    bounded_digests: set[bytes],
    candidate_item_ids: set[str],
    suppressed_item_ids: set[str],
    allow_legacy_function: bool,
) -> tuple[Any, bool]:
    return _tool_surface_adapter()._suppress_bounded_tool_search_calls_inner(
        value,
        bounded_digests,
        candidate_item_ids,
        suppressed_item_ids,
        allow_legacy_function,
    )


def _is_multi_agent_discovery_arguments(arguments: Mapping[str, Any] | None) -> bool:
    return _tool_surface_adapter().is_multi_agent_discovery_arguments(arguments)


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
    _collaboration_adapter().raise_worker_contract_error(
        event=event,
        error_code=error_code,
        classification=classification,
        surface=surface,
    )


def _validate_external_worker_selectors(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    _collaboration_adapter().validate_external_worker_selectors(
        value,
        event_context,
        surface=surface,
    )


def _worker_caller_carrier_supported(event_context: Mapping[str, Any] | None) -> bool:
    return _collaboration_adapter().worker_caller_carrier_supported(event_context)


def _requested_reasoning_effort(payload: Mapping[str, Any]) -> Any:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, Mapping):
        return reasoning.get("effort")
    if isinstance(reasoning, str):
        return reasoning
    return payload.get("reasoning_effort")


def _worker_requested_binding_signature_payload(binding: Mapping[str, Any], call_id: str) -> bytes:
    return _collaboration_adapter().requested_binding_signature_payload(binding, call_id)


def _requested_worker_binding_signature(binding: Mapping[str, Any], call_id: str) -> str:
    return _collaboration_adapter().requested_binding_signature(binding, call_id)


def _worker_requested_binding_sidecar(
    requested: Mapping[str, Any],
    call_id: str,
) -> dict[str, Any]:
    return _collaboration_adapter().requested_binding_sidecar(requested, call_id)


def _verified_worker_requested_binding(
    value: Any,
    call_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    return _collaboration_adapter().verified_requested_binding(value, call_id)


def _is_legacy_native_worker_spawn_call(
    item: Mapping[str, Any],
    arguments: Mapping[str, Any] | None,
) -> bool:
    return _collaboration_adapter().is_legacy_native_worker_spawn_call(item, arguments)


def _is_legacy_native_worker_spawn_readback(value: Any) -> bool:
    return _collaboration_adapter().is_legacy_native_worker_spawn_readback(value)


def _remember_worker_stream_item(
    state: dict[str, Any],
    item: Any,
    *,
    terminal: bool = False,
) -> None:
    _collaboration_adapter().remember_stream_item(state, item, terminal=terminal)


def _remember_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> None:
    _collaboration_adapter().remember_stream_event(value, event_context)


def _raise_on_invalid_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    _collaboration_adapter().raise_on_invalid_stream_event(
        value,
        event_context,
        surface=surface,
    )


def _attach_worker_requested_binding_sidecars(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    capture_stream_event: bool = True,
) -> tuple[Any, bool]:
    return _collaboration_adapter().attach_requested_binding_sidecars(
        value,
        event_context,
        capture_stream_event=capture_stream_event,
    )


def _apply_external_worker_response_contract(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    validate_selectors: bool = True,
    attach_sidecars: bool = True,
    capture_stream_event: bool = True,
) -> tuple[Any, bool]:
    return _collaboration_adapter().apply_external_worker_response_contract(
        value,
        event_context,
        surface=surface,
        validate_selectors=validate_selectors,
        attach_sidecars=attach_sidecars,
        capture_stream_event=capture_stream_event,
    )


def _validate_worker_binding_history(
    payload: Mapping[str, Any],
) -> bool:
    return _collaboration_adapter().validate_worker_binding_history(payload)


def _normalize_third_party_tool_call(
    value: Any,
    event_context: Mapping[str, Any] | None = None,
    compatibility_plan: Any = None,
) -> tuple[Any, bool]:
    return _tool_surface_adapter().normalize_third_party_tool_call(value, event_context, compatibility_plan)


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


def _downgrade_invalid_third_party_tool_calls(value: Any, compatibility_plan: Any = None) -> tuple[Any, bool]:
    return _tool_surface_adapter().downgrade_invalid_third_party_tool_calls(value, compatibility_plan)


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
    if spawn_allowed and subagent_state is None:
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
    _collaboration_adapter().reject_missing_worker_selector_for_generated_call(
        spec,
        event_context,
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
        if _validate_worker_binding_history(payload):
            changed = True
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
    pending_tool_surface_event: dict[str, Any] | None = None
    tool_surface_source_tools: list[Any] | None = None
    if not raw_provider_probe:
        # The selected tool-surface policy is a wire-shaping concern, not a
        # telemetry concern.  Apply it before runtime planning even when a
        # direct helper caller does not provide a mutable event context.  If
        # this remains behind the ``dict`` check, a runtime plan can see the
        # original namespace and re-expand every child into aliases (#425).
        if tool_surface_strategy == "deferred_core" or collaboration_v2:
            # ``additional_tools`` is an internal carrier.  Only deferred
            # external routes, or the client-owned V2 adapter, need it
            # promoted so namespace pruning/runtime planning can inspect the
            # declarations.  Ordinary eager routes must preserve this legacy
            # carrier byte-for-byte (#425).
            if _hoist_additional_tools_input_items(payload):
                changed = True
        if tool_surface_strategy == "deferred_core" and isinstance(payload.get("tools"), list):
            tools = payload["tools"]
            tool_surface_source_tools = list(tools)
            deferred_namespace_tools = [
                tool
                for tool in tools
                if _is_raw_namespace_schema(tool)
                and not (
                    collaboration_v2
                    and isinstance(tool, Mapping)
                    and tool.get("name") == _COLLABORATION_V2_NAMESPACE
                )
            ]
            retained_tools = [
                tool
                for tool in tools
                if not (
                    _is_raw_namespace_schema(tool)
                    and not (
                        collaboration_v2
                        and isinstance(tool, Mapping)
                        and tool.get("name") == _COLLABORATION_V2_NAMESPACE
                    )
                )
            ]
            if len(retained_tools) != len(tools):
                tools[:] = retained_tools
                changed = True
            if collaboration_v2:
                namespace_declaration_count, deferred_tool_count = (
                    _deferred_namespace_surface_counts(deferred_namespace_tools, retained_tools)
                )
                retained_tool_ids = {id(tool) for tool in retained_tools}
                pending_tool_surface_event = {
                    "tool_surface_strategy": tool_surface_strategy,
                    "namespace_declaration_count": namespace_declaration_count,
                    "eager_tool_count": 0,
                    "retained_core_count": sum(
                        1
                        for tool in tool_surface_source_tools
                        if not _is_raw_namespace_schema(tool)
                        and id(tool) in retained_tool_ids
                    ),
                    "deferred_tool_count": deferred_tool_count,
                }
        # Runtime planning is required for the wire transformation even when
        # this helper is used without a mutable telemetry context.  The
        # production handler supplies a dict so the plan/stream ledger can be
        # reused for response decoding, but direct callers and a few request
        # boundaries legitimately pass None or an immutable Mapping.  Using a
        # private context here prevents those calls from silently forwarding a
        # raw Collaboration namespace (or re-expanding deferred children) just
        # because telemetry storage was unavailable.
        # A real relay always supplies a mutable context.  The one context-free
        # path that still needs planning is the client-owned Collaboration V2
        # adapter: direct callers may omit telemetry, but the namespace must
        # still be converted before it can reach a third-party provider.  Keep
        # ordinary context-free compatibility calls on their legacy shaping
        # path; they have no response ledger to decode and changing them would
        # turn a helper call into a different protocol boundary.
        if isinstance(event_context, dict) or collaboration_v2:
            runtime_plan_context = (
                event_context if isinstance(event_context, dict) else {}
            )
            if _prepare_runtime_tool_compatibility(
                payload,
                upstream,
                tool_protocol,
                runtime_plan_context,
                native_responses_tool_codec=native_responses_tool_codec_override,
            ):
                changed = True
            runtime_tool_plan = _runtime_tool_compatibility_plan(runtime_plan_context)
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
        and _has_node_repl_subagent_plan_read_context(input_items)
    )
    subagent_workflow_plan_read_required = (
        not raw_provider_probe
        and subagent_state_active
        and subagent_state is not None
        and bool(getattr(subagent_state, "workflow_intent", False))
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
                and not bool(subagent_state.agents if subagent_state is not None else {})
            )
            if (
                tool_surface_strategy == "deferred_core"
                and include_node_repl_for_subagent_workflow
                and _restore_deferred_core_node_repl_namespace(
                    payload,
                    tool_surface_source_tools,
                )
            ):
                changed = True
                if runtime_tool_plan is not None:
                    runtime_tool_plan = runtime_tool_plan.with_final_declarations(
                        payload["tools"],
                        tool_choice=payload.get("tool_choice"),
                    )
                    if isinstance(event_context, dict):
                        event_context[_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY] = runtime_tool_plan
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
                deferred_core_surface=tool_surface_strategy == "deferred_core",
                tool_surface_counts=tool_surface_counts,
                tool_surface_source_tools=tool_surface_source_tools,
                open_agent_ids=open_agent_ids,
                wait_agent_ids=wait_agent_ids,
                close_agent_ids=close_agent_ids,
                worker_selector_values=(
                    ("worker", "default")
                    if worker_caller_carrier_supported
                    else ("default",)
                ),
            )
            if _restrict_bounded_tool_search_queries(payload, bounded_tool_search_queries):
                changed = True
            if tool_surface_counts:
                if runtime_tool_plan is not None and tool_surface_strategy == "eager":
                    tool_surface_counts["eager_tool_count"] = sum(
                        len(entry.aliases)
                        for entry in runtime_tool_plan.entries
                        if entry.family == "namespace"
                        and entry.disposition == "adapt"
                        and _is_flattened_namespace_schema(entry.declaration)
                    )
                    tool_surface_counts["deferred_tool_count"] = 0
                pending_tool_surface_event = {
                    "tool_surface_strategy": tool_surface_strategy,
                    **tool_surface_counts,
                }
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
    if runtime_tool_plan is not None:
        _write_runtime_tool_adapter_request_evidence(
            runtime_tool_plan,
            payload,
            event_context,
        )
    if pending_tool_surface_event is not None:
        final_tools = payload.get("tools")
        write_proxy_event(
            "external_tool_surface_prepared",
            **pending_tool_surface_event,
            final_tool_count=len(final_tools) if isinstance(final_tools, list) else 0,
        )
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


def _apply_patch_adapter_enabled(event_context: Mapping[str, Any] | None) -> bool:
    return _apply_patch_adapter().enabled(event_context)


def _adapt_apply_patch_custom_tool_history(
    input_items: list[Any],
    *,
    event_context: Mapping[str, Any] | None,
) -> tuple[list[Any], set[str], bool]:
    return _apply_patch_adapter().adapt_custom_tool_history(
        input_items,
        event_context=event_context,
    )


def _adapt_third_party_apply_patch_response_body(
    payload: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    return _apply_patch_adapter().adapt_response_body(payload, event_context)


class _ThirdPartyApplyPatchStreamAdapter(_ApplyPatchStreamAdapterImpl):
    def __init__(self, event_context: Mapping[str, Any] | None, *, surface: str = "stream"):
        super().__init__(_apply_patch_adapter(), event_context, surface=surface)


def _adapt_third_party_apply_patch_stream_events(
    events: list[Mapping[str, Any]],
    *,
    event_context: Mapping[str, Any] | None = None,
) -> tuple[list[Mapping[str, Any]], bool]:
    return _apply_patch_adapter().adapt_stream_events(events, event_context=event_context)


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
        wire_output = payload.get("output")
        try:
            decoded_payload = runtime_tool_plan.decode_payload(payload)
        except RuntimeToolCompatibilityError as exc:
            _raise_runtime_tool_compatibility_error(exc)
        _write_runtime_tool_adapter_response_evidence(
            runtime_tool_plan,
            wire_output if wire_output is not None else payload,
            decoded_payload.get("output") if isinstance(decoded_payload, Mapping) else decoded_payload,
            event_context,
            surface="body",
        )
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
    payload, alias_changed = _normalize_third_party_tool_call(payload, event_context, runtime_tool_plan)
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
    payload, invalid_tool_changed = _downgrade_invalid_third_party_tool_calls(payload, runtime_tool_plan)
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
    *,
    runtime_tool_inverse_only: bool = False,
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
    if not runtime_tool_inverse_only:
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
        wire_event = payload
        try:
            decoded_events = stream_state.decode_events_for_event(payload)
        except RuntimeToolCompatibilityError as exc:
            _raise_runtime_tool_compatibility_error(exc)
        _write_runtime_tool_adapter_response_evidence(
            runtime_tool_plan,
            wire_event,
            decoded_events,
            event_context,
            surface="sse",
        )
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

    if runtime_tool_inverse_only:
        if not runtime_tool_changed:
            return line
        return _sse_json_line(payload, line_ending) + line_ending

    if _is_raw_reasoning_stream_event(payload):
        return b""

    changed = _hide_reasoning_text(payload) or runtime_tool_changed
    payload, _ = _apply_external_worker_response_contract(
        payload,
        event_context,
        surface="sse",
        attach_sidecars=False,
    )
    payload, alias_changed = _normalize_third_party_tool_call(payload, event_context, runtime_tool_plan)
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
    payload, invalid_tool_changed = _downgrade_invalid_third_party_tool_calls(payload, runtime_tool_plan)
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
    explicit_phase = _explicit_transport_phase(exc)
    if explicit_phase is not None:
        return explicit_phase
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



from route_plan import (
    ToolExposurePolicy,
    VisionPlan,
    RouteRuntimeFacts,
    RetryExecutionPlan,
    RelayExecutionPlan,
    RouteAttemptPlan,
    RouteFailureObservation,
    RouteCapabilityBinding,
    RoutePlan,
    behavior_profile_for_request,
    route_plan_for_request,
    _route_runtime_facts,
    _route_plan_event_fields,
    _safe_route_endpoint_url,
    _is_codex_app_context,
    _has_explicit_third_party_client_identity,
    OFFICIAL_PASSTHROUGH_FIRST_EVENT_ATTEMPTS,
    _wire_format_adapter,
    _route_protocol,
    _route_provider_id,
    _validate_route_identity,
    _authentication_strategy,
    _route_endpoint_url,
    _capability_state,
    CAPABILITY_MANIFEST_VERSION_RE,
    SUPPORTED_CAPABILITY_MANIFEST_VERSIONS,
    CAPABILITY_MANIFEST_HASH_LENGTHS,
    _valid_capability_manifest_version,
    _valid_capability_manifest_hash,
    _capability_manifest_identity,
    CAPABILITY_BINDING_SCHEMA_VERSION,
    _binding_text,
    _canonical_binding_provider,
    _canonical_binding_model,
    _route_capability_binding,
    _default_route_runtime_facts,
    _vision_plan_for_route,
    _route_supports_transparent_metering,
    _tool_exposure_policy_for_route,
    _route_attempt_event_fields,
    TOOL_SURFACE_STRATEGY_ERROR_CODE,
    NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE,
    RESPONSE_ENDPOINT_SUFFIXES,
    KNOWN_UPSTREAM_ENDPOINT_SUFFIXES,
    _upstream_endpoint_url,
    _upstream_endpoint_root,
    _upstream_base_path_matches,
    _upstream_base_has_version_suffix,
    _responses_url,
    _chat_completions_url,
    _external_tool_protocol,
    _external_tool_surface_strategy,
    _external_native_responses_tool_codec,
)
import route_plan as _route_plan_module

def _forward_planning_event(event: str, **fields: Any) -> None:
    write_proxy_event(event, **fields)


_route_plan_module._planning_event_sink = _forward_planning_event

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
        elif key == "body_bytes":
            renamed[f"{prefix}_body_bytes"] = value
        elif key == "body_sha256":
            renamed[f"{prefix}_body_sha256"] = value
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


def _gateway_transport() -> GatewayTransport:
    """Build a request-time adapter so official/urlopen/token/sleep patches stay live."""
    return GatewayTransport(
        facts=TransportFacts(
            hop_by_hop_request_headers=frozenset(HOP_BY_HOP_REQUEST_HEADERS),
            official_alias_prefix=OFFICIAL_ALIAS_PREFIX,
            official_responses_lite_unsupported_models=frozenset(OFFICIAL_RESPONSES_LITE_UNSUPPORTED_MODELS),
            official_passthrough_behavior=BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            official_upstream_name="official",
            suppressed_retry_safety_classes=_SUPPRESSED_RETRY_SAFETY_CLASSES,
            downstream_closed_before_retry_error=DownstreamClosedBeforeRetryError,
        ),
        official_open=_official_urlopen,
        standard_open=urlopen,
        open_once_hook=_open_upstream_once,
        sleep=_sleep_for_retry_with_gateway_cancellation,
        active_request=_active_gateway_request,
        access_token=codex_access_token,
        account_id=codex_account_id,
        diagnostic_recorder=GATEWAY_DIAGNOSTIC_RECORDER,
        diagnostic_context_value=_diagnostic_context_value,
        diagnostic_connection_disposition=_diagnostic_connection_disposition,
        diagnostic_error_connection_disposition=_diagnostic_error_connection_disposition,
        diagnostic_response_metadata=_diagnostic_response_metadata,
        diagnostic_transport_phase=_diagnostic_transport_phase,
        emit_retry=_emit_upstream_retry_event,
        emit_retry_suppressed=_emit_upstream_retry_suppressed_event,
        retry_delay_seconds=gateway_retry_delay_seconds,
        failure_class_hook=_upstream_failure_class,
        retry_after_hook=_retry_after_delay_seconds,
        retry_attempts_for_failure_class=_retry_attempts_for_failure_class,
        capacity_elapsed_allows=_capacity_retry_elapsed_limit_allows,
        retry_safety_class=_retry_safety_class,
        retry_safety_failure_phase=_retry_safety_failure_phase,
        failure_phase=transport_failure_phase,
        model_access_path=_model_access_path_from_event_context,
        model_access_path_idempotent=_model_access_path_idempotency_guaranteed,
        ensure_retry_identity=_ensure_retry_attempt_identity,
        retry_identity_from_context=_retry_identity_from_context,
        downstream_retry_payload=_downstream_retry_payload,
        get_header=_get_header,
        header_items=_header_items,
        upstream_retry_attempts=_upstream_retry_attempts,
        getproxies=getproxies,
        getproxies_registry=getproxies_registry,
        proxy_bypass=proxy_bypass,
        platform=sys.platform,
        official_pools=OFFICIAL_HTTP_POOLS,
        official_pools_lock=OFFICIAL_HTTP_POOLS_LOCK,
        pool_manager_hook=_official_pool_manager,
        proxy_url_hook=_official_proxy_url,
        endpoint_url_hook=_upstream_endpoint_url,
    )


def materialize_operational_authentication(
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
) -> OperationalAuthentication:
    return _gateway_transport().materialize_authentication(incoming_headers, upstream)


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
    return _gateway_transport().build_headers(
        incoming_headers,
        upstream,
        drop_content_encoding=drop_content_encoding,
        behavior_profile=behavior_profile,
        model_id=model_id,
        authentication_strategy=authentication_strategy,
        request_mutation_policy=request_mutation_policy,
        operational_authentication=operational_authentication,
    )


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
    return _catalog_runtime().current_catalog_data()


def openai_model_list(catalog: Mapping[str, Any]) -> dict[str, Any]:
    return CatalogRuntime.openai_model_list(catalog)


def published_official_context_budgets(catalog_path: Path) -> dict[str, Mapping[str, Any]]:
    return _catalog_runtime().published_official_context_budgets(catalog_path)


def catalog_with_openai_context_guard(
    catalog: dict[str, Any],
    published_budgets: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    require_published_snapshot: bool = False,
) -> dict[str, Any]:
    return _catalog_runtime().catalog_with_openai_context_guard(
        catalog,
        published_budgets,
        require_published_snapshot=require_published_snapshot,
    )


def catalog_with_vision_proxy_capabilities(catalog: dict[str, Any]) -> dict[str, Any]:
    return _catalog_runtime().catalog_with_vision_proxy_capabilities(catalog)


def catalog_with_official_fast_variants(catalog: dict[str, Any]) -> dict[str, Any]:
    return _catalog_runtime().catalog_with_official_fast_variants(catalog)


def canonical_catalog_models(
    models: list[Any],
    policy: CatalogPolicy,
) -> list[Any]:
    return _catalog_runtime().canonical_catalog_models(models, policy)


def _json_response_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


def _modalities_include_image(value: Any) -> bool:
    return CatalogRuntime.modalities_include_image(value)


def _catalog_input_modalities(
    model_id: str | None,
    upstream: Mapping[str, Any] | None = None,
) -> Any:
    return _catalog_runtime().catalog_input_modalities(model_id, upstream)


def model_supports_image(
    model_id: str | None,
    upstream: Mapping[str, Any] | None = None,
) -> bool:
    return _catalog_runtime().model_supports_image(model_id, upstream)


_CATALOG_ORIGINAL_OFFICIAL_BASE_URL = official_base_url
_CATALOG_ORIGINAL_OLLAMA_BASE_URL = ollama_cloud_base_url
_CATALOG_ORIGINAL_FAST_VARIANTS = catalog_with_official_fast_variants
_CATALOG_ORIGINAL_CONTEXT_GUARD = catalog_with_openai_context_guard
_CATALOG_ORIGINAL_VISION_PROJECTION = catalog_with_vision_proxy_capabilities
_CATALOG_ORIGINAL_CANONICAL_MODELS = canonical_catalog_models
_CATALOG_ORIGINAL_MODALITIES = _modalities_include_image
_CATALOG_ORIGINAL_INPUT_MODALITIES = _catalog_input_modalities
_CATALOG_ORIGINAL_BY_SLUG = generated_catalog_by_slug
_CATALOG_ORIGINAL_PUBLISHED_BUDGETS = published_official_context_budgets


def _catalog_override(candidate: Callable[..., Any], original: Callable[..., Any]) -> Callable[..., Any] | None:
    wrapped = getattr(candidate, "__wrapped__", None)
    return None if candidate is original or wrapped is original else candidate


def _is_image_part(value: Any) -> bool:
    return _vision_proxy_adapter().is_image_part(value)


def _value_contains_image(value: Any) -> bool:
    return _vision_proxy_adapter().value_contains_image(value)


def _normalized_vision_image_part(part: Mapping[str, Any]) -> dict[str, Any]:
    return _vision_proxy_adapter().normalized_image_part(part)


def _image_proxy_cache_key(part: Mapping[str, Any], vision_model: str) -> str:
    return _vision_proxy_adapter().cache_key(part, vision_model)


def _image_proxy_unique_image_count(value: Any, vision_model: str) -> int:
    return _vision_proxy_adapter().unique_image_count(value, vision_model)


def _image_proxy_cache_lookup(cache_key: str) -> str | None:
    return _vision_proxy_adapter().cache_lookup(cache_key)


def _image_proxy_cache_store(
    cache_key: str,
    vision_model: str,
    description: str,
) -> None:
    _vision_proxy_adapter().cache_store(cache_key, vision_model, description)


def _extract_model_response_text(payload: Any) -> str:
    return _vision_proxy_adapter().extract_response_text(payload)


def _image_proxy_response_body(response: Any) -> bytes:
    return _vision_proxy_adapter().response_body(response)


def _call_vision_model_for_image_description(
    part: Mapping[str, Any],
    vision_model: str,
    vision_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> str:
    return _vision_proxy_adapter().call_vision_model_for_description(
        part,
        vision_model,
        vision_upstream,
        event_context,
    )


def _image_proxy_description_for_part(
    part: Mapping[str, Any],
    vision_model: str,
    vision_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> str:
    return _vision_proxy_adapter().description_for_part(
        part,
        vision_model,
        vision_upstream,
        event_context,
    )


def _image_proxy_reference_for_part(
    part: Mapping[str, Any],
    vision_model: str,
) -> str:
    return _vision_proxy_adapter().image_reference(part, vision_model)


def _image_proxy_vision_upstream() -> tuple[str, Mapping[str, Any]]:
    return _vision_proxy_adapter().vision_upstream()


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
    return _vision_proxy_adapter().apply_responses_payload(
        payload,
        target_model,
        target_upstream,
        event_context=event_context,
        progress_callback=progress_callback,
        image_proxy_enabled=image_proxy_enabled,
        target_accepts_images=target_accepts_images,
    )


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
    return _vision_proxy_adapter().apply_chat_payload(
        payload,
        target_model,
        target_upstream,
        event_context=event_context,
        progress_callback=progress_callback,
        image_proxy_enabled=image_proxy_enabled,
        target_accepts_images=target_accepts_images,
    )


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
    try:
        inbound_protocol = RouteProtocol(inbound_format)
        policy = VisionProxyPolicy(vision_proxy_policy)
    except ValueError as exc:
        raise ImageProxyError("Vision Proxy received an unsupported Route Plan value") from exc
    return _vision_proxy_adapter().apply(
        payload,
        inbound_protocol=inbound_protocol,
        target_model=target_model,
        target_upstream=target_upstream,
        policy=policy,
        image_proxy_enabled=image_proxy_enabled,
        target_accepts_images=target_accepts_images,
        event_context=event_context,
        progress_callback=progress_callback,
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
    try:
        inbound_protocol = RouteProtocol(inbound_format)
    except ValueError as exc:
        raise ImageProxyError("Vision Proxy received an unsupported inbound protocol") from exc
    return _vision_proxy_adapter().enforce_text_only_boundary(
        payload,
        inbound_protocol=inbound_protocol,
        target_model=target_model,
        target_upstream=target_upstream,
        vision_plan=vision_plan,
        event_context=event_context,
        progress_callback=progress_callback,
    )


_VISION_ORIGINAL_CACHE_LOOKUP = _image_proxy_cache_lookup
_VISION_ORIGINAL_CACHE_STORE = _image_proxy_cache_store
_VISION_ORIGINAL_RESPONSE_BODY = _image_proxy_response_body
_VISION_ORIGINAL_EXTRACT_TEXT = _extract_model_response_text
_VISION_ORIGINAL_DESCRIBE_IMAGE = _call_vision_model_for_image_description
_VISION_ORIGINAL_DESCRIPTION_FOR_PART = _image_proxy_description_for_part
_VISION_ORIGINAL_UPSTREAM = _image_proxy_vision_upstream
_VISION_ORIGINAL_APPLY_RESPONSES = apply_image_proxy_to_responses_payload
_VISION_ORIGINAL_APPLY_CHAT = apply_image_proxy_to_chat_payload
_VISION_ORIGINAL_BOUNDARY = apply_vision_proxy_adapter


def _vision_proxy_override(candidate: Callable[..., Any], original: Callable[..., Any]) -> Callable[..., Any] | None:
    return None if candidate is original else candidate


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
    """Compatibility adapter from HTTP input to the typed inbound seam."""
    return parse_inbound_request(
        InboundRequest(
            request_id=request_id,
            started_at=started_at,
            path=handler.path,
            protocol=RouteProtocol(inbound_format),
            provider_hint=provider_hint,
            headers=handler.headers,
            body=handler.rfile.read(content_length),
            request_context=request_context,
            proxy_request_context=proxy_request_context,
            raw_provider_probe=raw_provider_probe,
        ),
        InboundRequestHooks(
            decode_body=decoded_request_body,
            get_header=_get_header,
            request_kind=_request_kind_from_headers_and_payload,
            compact_request_kind=RETRY_REQUEST_COMPACT,
            event_context_for_kind=_event_context_with_request_kind,
            try_extract_model=try_extract_model,
            provider_scoped_model=provider_scoped_route_model,
        ),
    )


def _open_upstream_once(
    request: Request,
    *,
    upstream_name: str,
    timeout: int | float,
    transport_policy: TransportPolicy | None = None,
) -> Any:
    return _gateway_transport().open_once(
        request,
        upstream_name=upstream_name,
        timeout=timeout,
        transport_policy=transport_policy,
    )


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
    return _gateway_transport().open_response(
        request,
        upstream_name=upstream_name,
        upstream_format=upstream_format,
        timeout=timeout,
        event_context=event_context,
        downstream_retry_callback=downstream_retry_callback,
        request_kind=request_kind,
        max_attempts=max_attempts,
        retry_policy=retry_policy,
        retry_http_errors=retry_http_errors,
        retry_execution=retry_execution,
        transport_policy=transport_policy,
        downstream_exposed=downstream_exposed,
        pre_response_deadline=pre_response_deadline,
        open_attempt_budget=open_attempt_budget,
    )


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
# ``write`` is _HandlerDownstreamIO.write, the handler-bound byte sink.
DOWNSTREAM_STREAM_COMMIT_SEAM_METHODS: frozenset[str] = frozenset({
    "commit_data",
    "commit_sse_bytes",
    "commit_terminal_failure",
    "write",
})


def _handler_downstream_stream_commit(handler: Any) -> DownstreamStreamCommit | None:
    """Return the request-scoped stream-commit seam bound to ``handler`` if active."""
    seam = getattr(handler, "_downstream_stream_commit", None)
    return seam if isinstance(seam, DownstreamStreamCommit) else None


class _HandlerDownstreamIO:
    """Adapter from CodexProxyHandler to DownstreamStreamCommit write/flush/close."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def write(self, data: bytes) -> None:
        self._handler.wfile.write(data)

    def flush(self) -> None:
        self._handler.wfile.flush()

    def close(self) -> None:
        self._handler.close_connection = True


def _bind_handler_synthetic_terminal_failure(
    handler: Any,
    callback: Callable[..., tuple[bool, str | None, str | None]] | None,
    *,
    redact_identity: str | None = None,
) -> Callable[..., tuple[bool, str | None, str | None]] | None:
    if callback is None:
        return None

    def bound(
        exc: BaseException,
        *,
        status: int,
        response_id: str | None,
        upstream_name: str,
        model: str | None,
    ) -> tuple[bool, str | None, str | None]:
        return callback(
            handler,
            exc,
            status=status,
            response_id=response_id,
            upstream_name=upstream_name,
            model=model,
            redact_identity=redact_identity,
        )

    return bound


def _bind_downstream_stream_commit(
    handler: Any,
    upstream: Any | None,
    upstream_name: str,
    **kwargs: Any,
) -> DownstreamStreamCommit:
    redact_identity = kwargs.pop("redact_identity", None)
    kwargs.setdefault("usage_line_callback", _offer_official_passthrough_usage_line)
    kwargs.setdefault("diagnostic_observer", _observe_gateway_diagnostic)
    kwargs.setdefault("terminal_observer", _responses_terminal_observer)
    kwargs.setdefault(
        "output_observer",
        lambda event: _responses_event_commits_downstream_output(event, ""),
    )
    kwargs.setdefault(
        "error_detail_callback",
        lambda exc: safe_upstream_error_detail(exc, redact_identity=redact_identity),
    )
    kwargs.setdefault(
        "terminal_drain_timeout_seconds",
        OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS,
    )
    if "synthetic_terminal_failure_callback" in kwargs:
        kwargs["synthetic_terminal_failure_callback"] = (
            _bind_handler_synthetic_terminal_failure(
                handler,
                kwargs["synthetic_terminal_failure_callback"],
                redact_identity=redact_identity,
            )
        )
    return DownstreamStreamCommit(
        _HandlerDownstreamIO(handler),
        upstream,
        upstream_name,
        **kwargs,
    )


# Build explicit facade bindings per relay request so compatibility patches stay live.
def _relay_write_proxy_event(event: str, **fields: Any) -> None:
    write_proxy_event(event, **fields)

def _relay_compatible_sse_line(*args: Any, **kwargs: Any) -> Any:
    return compatible_sse_line(*args, **kwargs)


def _relay_symbols() -> RelaySymbols:
    return RelaySymbols(
            CompactEmptyResponseError=CompactEmptyResponseError,
            DownstreamKeepaliveFailedError=DownstreamKeepaliveFailedError,
            IncompleteRead=IncompleteRead,
            NonForwardable=NonForwardable,
            RuntimeToolCompatibilityError=RuntimeToolCompatibilityError,
            SseFrameTooLargeError=SseFrameTooLargeError,
            URLError=URLError,
            UpstreamEmptyCompletedResponseError=UpstreamEmptyCompletedResponseError,
            UpstreamProtocolTranslationError=UpstreamProtocolTranslationError,
            UpstreamSseSemanticError=UpstreamSseSemanticError,
            UpstreamStreamErrorEvent=UpstreamStreamErrorEvent,
            UpstreamStreamIdleTimeoutError=UpstreamStreamIdleTimeoutError,
            UpstreamStreamIncompleteError=UpstreamStreamIncompleteError,
            UpstreamStreamInterruptedError=UpstreamStreamInterruptedError,
            DownstreamStreamCommit=DownstreamStreamCommit,
            MutationPolicy=MutationPolicy,
            PreparedExchange=PreparedExchange,
            StreamingPolicy=StreamingPolicy,
            UsagePolicy=UsagePolicy,
            _ChatToResponsesStreamConverter=_ChatToResponsesStreamConverter,
            _ResponsesToChatStreamConverter=_ResponsesToChatStreamConverter,
            _ThirdPartyApplyPatchStreamAdapter=_ThirdPartyApplyPatchStreamAdapter,
            _RuntimeToolInverseStreamError=_RuntimeToolInverseStreamError,
            RETRY_FAILURE_QUICK_TRANSIENT=RETRY_FAILURE_QUICK_TRANSIENT,
            RETRY_REQUEST_COMPACT=RETRY_REQUEST_COMPACT,
            RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE=RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
            RETRY_SAFETY_SUPPRESSED_POST_WRITE=RETRY_SAFETY_SUPPRESSED_POST_WRITE,
            _adapt_third_party_apply_patch_stream_events=_adapt_third_party_apply_patch_stream_events,
            _apply_external_worker_response_contract=_apply_external_worker_response_contract,
            _apply_patch_adapter_enabled=_apply_patch_adapter_enabled,
            _bind_downstream_stream_commit=_bind_downstream_stream_commit,
            _bounded_failure_event_context=_bounded_failure_event_context,
            _capture_usage=_capture_usage,
            _chat_completion_body_is_empty=_chat_completion_body_is_empty,
            _chat_completion_body_to_stream_chunks=_chat_completion_body_to_stream_chunks,
            _chat_completion_to_response_body=_chat_completion_to_response_body,
            _chat_sse_event_resets_idle_timeout=_chat_sse_event_resets_idle_timeout,
            _chat_stream_chunks_have_terminal=_chat_stream_chunks_have_terminal,
            _chat_stream_chunks_to_response_events=_chat_stream_chunks_to_response_events,
            _chat_stream_error_detail=_chat_stream_error_detail,
            _chat_stream_lifecycle_final_issue=_chat_stream_lifecycle_final_issue,
            _chat_stream_shape_summary=_chat_stream_shape_summary,
            _chat_terminal_observer=_chat_terminal_observer,
            _coerce_exact_spawn_prompt_tool_calls=_coerce_exact_spawn_prompt_tool_calls,
            _coerce_required_subagent_tool_calls=_coerce_required_subagent_tool_calls,
            _compact_response_body_is_empty=_compact_response_body_is_empty,
            _converted_sse_payload=_converted_sse_payload,
            _count_sse_reasoning_event=_count_sse_reasoning_event,
            _downgrade_invalid_third_party_tool_calls=_downgrade_invalid_third_party_tool_calls,
            _events_to_responses_body=_events_to_responses_body,
            _filtered_response_headers=_filtered_response_headers,
            _guard_duplicate_multi_agent_spawn_calls=_guard_duplicate_multi_agent_spawn_calls,
            _handler_downstream_stream_commit=_handler_downstream_stream_commit,
            _incomplete_stream_json_error_body=_incomplete_stream_json_error_body,
            _is_event_stream=_is_event_stream,
            _is_reasoning_summary_stream_event=_is_reasoning_summary_stream_event,
            _is_sse_blank_line=_is_sse_blank_line,
            _is_sse_event_metadata_line=_is_sse_event_metadata_line,
            _json_error_payload_for_inbound_format=_json_error_payload_for_inbound_format,
            _lifecycle_final_issue_event_name=_lifecycle_final_issue_event_name,
            _lifecycle_final_issue_missing_reason=_lifecycle_final_issue_missing_reason,
            _normalize_third_party_tool_call=_normalize_third_party_tool_call,
            _observe_gateway_diagnostic=_observe_gateway_diagnostic,
            _offer_usage_observed_body=_offer_usage_observed_body,
            _offer_usage_observed_sse_line=_offer_usage_observed_sse_line,
            _parse_sse_json_payload=_parse_sse_json_payload,
            _parse_sse_json_payloads=_parse_sse_json_payloads,
            _public_event_context=_public_event_context,
            _raise_lifecycle_final_issue=_raise_lifecycle_final_issue,
            _raise_runtime_tool_compatibility_error=_raise_runtime_tool_compatibility_error,
            _reconcile_function_call_argument_events=_reconcile_function_call_argument_events,
            _redact_identity_in_text=_redact_identity_in_text,
            _repair_missing_required_subagent_call_events=_repair_missing_required_subagent_call_events,
            _response_body_lifecycle_final_issue=_response_body_lifecycle_final_issue,
            _response_body_to_chat_completion_body=_response_body_to_chat_completion_body,
            _response_body_to_response_sse_events=_response_body_to_response_sse_events,
            _response_events_shape_summary=_response_events_shape_summary,
            _responses_body_is_empty=_responses_body_is_empty,
            _responses_completed_tool_item=_responses_completed_tool_item,
            _responses_event_commits_downstream_output=_responses_event_commits_downstream_output,
            _responses_event_has_visible_or_tool_output=_responses_event_has_visible_or_tool_output,
            _responses_event_is_tool_call_construction=_responses_event_is_tool_call_construction,
            _responses_event_starts_downstream_output=_responses_event_starts_downstream_output,
            _responses_events_have_terminal=_responses_events_have_terminal,
            _responses_events_lifecycle_final_issue=_responses_events_lifecycle_final_issue,
            _responses_failed_event_for_stream_error=_responses_failed_event_for_stream_error,
            _responses_sse_event_resets_idle_timeout=_responses_sse_event_resets_idle_timeout,
            _responses_sse_line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
            _responses_stream_error_detail=_responses_stream_error_detail,
            _responses_stream_error_type=_responses_stream_error_type,
            _responses_terminal_observer=_responses_terminal_observer,
            _retry_identity_from_context=_retry_identity_from_context,
            _route_failure_event_fields=_route_failure_event_fields,
            _runtime_tool_compatibility_stream_for_attempt=_runtime_tool_compatibility_stream_for_attempt,
            _sse_event_separator_after_line=_sse_event_separator_after_line,
            _sse_json_line=_sse_json_line,
            _sse_line_ending=_sse_line_ending,
            _suppress_bounded_tool_search_calls=_suppress_bounded_tool_search_calls,
            _suppress_chat_reasoning_extensions=_suppress_chat_reasoning_extensions,
            _suppress_coordinator_forbidden_tool_calls=_suppress_coordinator_forbidden_tool_calls,
            _suppress_worker_multi_agent_tool_calls=_suppress_worker_multi_agent_tool_calls,
            _synthetic_response_completed_from_tool_items=_synthetic_response_completed_from_tool_items,
            _upstream_failure_class=_upstream_failure_class,
            _usage_from_json_body=_usage_from_json_body,
            _usage_from_payload=_usage_from_payload,
            _usage_from_response_event=_usage_from_response_event,
            _usage_observed_context=_usage_observed_context,
            _verified_converted_sse_semantic_error=_verified_converted_sse_semantic_error,
            _with_codexhub_http_error=_with_codexhub_http_error,
            _write_adapter_event=_write_adapter_event,
            _write_runtime_tool_adapter_response_evidence=_write_runtime_tool_adapter_response_evidence,
            compatible_response_body=compatible_response_body,
            compatible_sse_line=_relay_compatible_sse_line,
            safe_upstream_error_detail=safe_upstream_error_detail,
            write_proxy_event=_relay_write_proxy_event,
    )

_UpstreamSseReaderLifecycle = UpstreamSseReaderLifecycle


class CodexProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _active_prepared_exchange: PreparedExchange | None = None

    def handle_one_request(self) -> None:
        self._active_prepared_exchange: PreparedExchange | None = None
        self._diagnostic_request_id: str | None = None
        try:
            super().handle_one_request()
        finally:
            self._active_prepared_exchange = None
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
            self._send_json(200, openai_model_list(current_catalog_data()))
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

        def send_user_requested_shutdown() -> None:
            _record_user_requested_shutdown()
            self._send_json_and_close(503, user_requested_shutdown_payload("responses"))

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
            send_user_requested_shutdown()
            return
        previous_admission = _activate_gateway_request(admission)
        upstream_name = "official"
        status = 500
        try:
            admission.raise_if_cancelled()
            upstream = official_upstream()
            upstream_name = str(upstream["name"])
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
            request = _gateway_transport().build_request(
                upstream,
                "/images/generations",
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
            send_user_requested_shutdown()
        except (IncompleteRead, OSError, URLError) as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
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
            if admission.cancelled:
                send_user_requested_shutdown()
                return
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
            collaboration_protocol = (
                _resolve_collaboration_boundary(
                    inbound_payload,
                    proxy_request_context,
                    surface="request",
                )
                if provider_hint is not None and upstream_name != "official"
                else None
            )
            route_plan = route_plan_for_request(
                upstream,
                request_context,
                inbound_format=inbound_format,
                provider_hint=provider_hint,
                collaboration_protocol=collaboration_protocol,
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
            model_canonical = canonical_model_id(model) if model else None
            # Create the request-scoped downstream stream-commit seam early so that
            # every production SSE header/body (status, retry, keepalive, converted
            # output, error, terminal) is authorized by the same lifecycle owner.
            # The upstream response is attached once it is opened.
            self._downstream_stream_commit = _bind_downstream_stream_commit(
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
            vision_proxy_payload_format = (
                route_plan.prepared_request_protocol.value
            )
            image_proxy_payload: dict[str, Any] | None = None
            if route_plan.vision.action in {VisionAction.PROXY, VisionAction.REJECT}:
                try:
                    parsed_image_proxy_payload = json.loads(body.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_image_proxy_payload = None
                if isinstance(parsed_image_proxy_payload, dict):
                    image_proxy_payload = parsed_image_proxy_payload
            try:
                if route_plan.vision.action in {VisionAction.PROXY, VisionAction.REJECT}:
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
            prepared_caller_body = body

            def request_observability_for_attempt(
                attempt: RouteAttemptPlan,
                attempt_body: bytes,
            ) -> dict[str, Any]:
                upstream_observability = proxy_telemetry.enrich_request_observability(
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
                return {
                    **upstream_observability,
                    **_request_observability_with_prefix(
                        caller_request_observability, "caller"
                    ),
                    **_request_observability_with_prefix(
                        upstream_observability, "upstream"
                    ),
                    "request_observability_scope": "executed_attempt",
                    "request_observability_attempt_index": attempt.index,
                    "request_observability_upstream_protocol": (
                        attempt.upstream_protocol.value
                    ),
                }

            def set_active_exchange(exchange: PreparedExchange) -> None:
                self._active_prepared_exchange = exchange

            def activate_route_attempt(attempt: RouteAttemptPlan) -> None:
                attempt_fields = _route_attempt_event_fields(
                    attempt,
                    provider_id=route_plan.provider_id,
                    model_requested=route_plan.model_requested,
                    model_canonical=route_plan.canonical_model,
                    upstream_model=route_plan.upstream_model,
                )
                proxy_request_context.update(attempt_fields)
                adapter_event_context.update(attempt_fields)
                adapter_event_context["tool_protocol"] = attempt.tool_protocol

            def rewrite_developer_roles(
                attempt_body: bytes,
                compatibility_upstream: Mapping[str, Any],
            ) -> tuple[bytes, int]:
                mutated, rewrites = _rewrite_transparent_developer_role_messages(
                    attempt_body, compatibility_upstream
                )
                if rewrites:
                    write_proxy_event(
                        "developer_role_rewrite_applied",
                        request_id=request_id,
                        model=model_canonical,
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        messages_rewritten=rewrites,
                        **proxy_request_context,
                    )
                return mutated, rewrites

            def normalize_tool_schema_booleans(
                attempt_body: bytes,
            ) -> tuple[bytes, int]:
                mutated, rewrites = _normalize_transparent_tool_schema_booleans(
                    attempt_body
                )
                if rewrites:
                    write_proxy_event(
                        "tool_schema_boolean_normalized",
                        request_id=request_id,
                        model=model_canonical,
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        schemas_rewritten=rewrites,
                        **proxy_request_context,
                    )
                return mutated, rewrites

            def validate_transparent_tool_loop(
                attempt_body: bytes,
                attempt_format: str,
            ) -> None:
                transparent_payload = _safe_json_mapping(attempt_body) or {}
                repeated_count = (
                    _excessive_transparent_responses_tool_loop_count(
                        transparent_payload
                    )
                    if attempt_format == "responses"
                    else _excessive_transparent_chat_tool_loop_count(
                        transparent_payload
                    )
                    if attempt_format == "chat_completions"
                    else None
                )
                if repeated_count is not None:
                    raise UpstreamProtocolTranslationError(
                        UnsupportedProtocolTranslationError(
                            EXCESSIVE_TOOL_LOOP_ERROR_CODE,
                            "Repeated successful function calls exceeded "
                            f"the bound of {EXCESSIVE_TOOL_LOOP_BOUND}.",
                        )
                    )

            def build_attempt_request(
                attempt: RouteAttemptPlan,
                attempt_body: bytes,
            ) -> Request:
                return _gateway_transport().build_request_url(
                    attempt.endpoint_url,
                    data=attempt_body,
                    headers=attempt.request_headers.to_dict(),
                    method="POST",
                )

            def lifecycle_guidance(
                attempt_body: bytes,
                reason: str,
            ) -> bytes:
                guided = _responses_body_with_lifecycle_final_retry_guidance(
                    attempt_body, reason
                )
                _write_adapter_event(
                    adapter_event_context,
                    "lifecycle_final_retry_guidance_injected",
                    upstream=upstream_name,
                    upstream_format="responses",
                    reason=reason,
                )
                return guided

            emit_retry_to_downstream = (
                primary_route_attempt.retry.emit_downstream_retry_notice
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
                        "route_mode": (
                            "official"
                            if upstream_name == "official"
                            else "codexhub"
                        ),
                        "inbound_format": inbound_format,
                        "is_stream": caller_stream,
                    }
                )
                retry_payload = dict(payload)
                retry_payload.pop("type", None)
                notice_fields.update(retry_payload)
                write_proxy_event("sse_retry_notice", **notice_fields)
                return True

            def set_upstream_format(selected_format: str) -> None:
                seam = _handler_downstream_stream_commit(self)
                if seam is not None:
                    seam.set_upstream_format(selected_format)

            def attach_upstream(response: Any) -> None:
                seam = _handler_downstream_stream_commit(self)
                if seam is not None:
                    seam.attach_upstream(response)

            def open_exchange(
                opening: OpenExchangeRequest,
            ) -> Any:
                return _open_upstream_response(
                    opening.request,
                    upstream_name=opening.upstream_name,
                    upstream_format=opening.upstream_format,
                    timeout=opening.attempt.retry.request_timeout_seconds,
                    event_context=opening.event_context,
                    downstream_retry_callback=(
                        opening.downstream_retry_callback
                    ),
                    retry_execution=opening.attempt.retry,
                    transport_policy=opening.attempt.transport_policy,
                    downstream_exposed=opening.downstream_exposed,
                    pre_response_deadline=opening.pre_response_deadline,
                    open_attempt_budget=opening.open_attempt_budget,
                )

            def relay_exchange(
                response: Any,
                relay: RelayExchangeRequest,
            ) -> int:
                return self._relay_upstream_response(
                    response,
                    relay.upstream_name,
                    request_id=relay.request_id,
                    model=relay.model,
                    inbound_format=relay.inbound_format,
                    caller_stream=relay.caller_stream,
                    event_context=relay.event_context,
                    usage_capture=relay.usage_capture,
                    headers_already_sent=relay.headers_already_sent,
                    defer_stream_errors=relay.defer_stream_errors,
                    mark_downstream_sse_started=(
                        relay.mark_downstream_sse_started
                    ),
                    response_lifecycle_state=relay.response_lifecycle_state,
                    relay_execution_plan=relay.relay_plan,
                )

            def raise_if_cancelled() -> None:
                active_request = _active_gateway_request()
                if active_request is not None:
                    active_request.raise_if_cancelled()

            def protocol_fallback(
                failed_attempt: RouteAttemptPlan,
                next_attempt: RouteAttemptPlan,
                exc: BaseException,
                attempt_observability: Mapping[str, Any],
            ) -> None:
                failed = failed_attempt.telemetry_snapshot()
                following = next_attempt.telemetry_snapshot()
                write_proxy_event(
                    "upstream_protocol_fallback",
                    request_id=request_id,
                    model=model_canonical,
                    model_requested=model_requested,
                    model_canonical=model_canonical,
                    upstream=upstream_name,
                    provider_id=upstream_name,
                    provider_hint=provider_hint,
                    upstream_format=(
                        route_plan.configured_upstream_protocol_name
                    ),
                    behavior_profile=behavior_profile,
                    failed_upstream_format=(
                        failed_attempt.selected_upstream_format
                    ),
                    next_upstream_format=(
                        next_attempt.selected_upstream_format
                    ),
                    failed_route_attempt_index=failed["index"],
                    failed_route_attempt_request_body_mode=(
                        failed["request_body_mode"]
                    ),
                    failed_route_attempt_request_conversion_steps=(
                        failed["request_conversion_steps"]
                    ),
                    failed_route_attempt_mutation_summary=(
                        failed["mutation_summary"]
                    ),
                    next_route_attempt_index=following["index"],
                    next_route_attempt_request_body_mode=(
                        following["request_body_mode"]
                    ),
                    next_route_attempt_request_conversion_steps=(
                        following["request_conversion_steps"]
                    ),
                    next_route_attempt_mutation_summary=(
                        following["mutation_summary"]
                    ),
                    status=getattr(exc, "code", 502),
                    error="HTTPError",
                    detail=safe_upstream_error_detail(
                        exc,
                        redact_identity=_retry_identity_from_context(
                            adapter_event_context
                        ),
                    ),
                    **dict(attempt_observability),
                    **proxy_request_context,
                )

            def exchange_error_status(exc: BaseException) -> int | None:
                status_value = getattr(exc, "code", None)
                return status_value if isinstance(status_value, int) else None

            def handle_empty_completed(exc: BaseException) -> bool:
                detail = (
                    "Upstream Responses stream completed without visible "
                    "output or tool calls."
                )
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
                wrote = self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_empty_completed_response",
                    detail=detail,
                    redact_identity=_retry_identity_from_context(
                        adapter_event_context
                    ),
                )
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="empty_completed_response",
                )
                return wrote

            exchange_progress = ExchangeProgress(
                upstream_format=upstream_format,
                downstream_sse_started=downstream_sse_started,
            )
            exchange_request = ExchangeRequest(
                inbound=request_input,
                route_plan=route_plan,
                upstream=upstream,
                upstream_name=upstream_name,
                prepared_body=prepared_caller_body,
                inbound_payload=inbound_payload,
                model_canonical=model_canonical,
                caller_stream=caller_stream,
                prompt_cache_key=prompt_cache_key,
                caller_request_observability=caller_request_observability,
                event_context=adapter_event_context,
                proxy_request_context=proxy_request_context,
                usage_capture=usage_capture,
                response_lifecycle_state=response_lifecycle_state,
                pre_response_deadline=self._pre_response_deadline,
                downstream_sse_started=downstream_sse_started,
            )
            exchange_hooks = ExchangeHooks(
                failure_types=ExchangeFailureTypes(
                    downstream_closed_before_retry=(
                        DownstreamClosedBeforeRetryError
                    ),
                    incomplete_read=IncompleteRead,
                    protocol_fallback_error=HTTPError,
                    compact_empty=CompactEmptyResponseError,
                    stream_interrupted=UpstreamStreamInterruptedError,
                    stream_idle_timeout=UpstreamStreamIdleTimeoutError,
                    stream_incomplete=UpstreamStreamIncompleteError,
                    stream_error_event=UpstreamStreamErrorEvent,
                    lifecycle_empty_final=LifecycleEmptyFinalResponseError,
                    lifecycle_final_format=LifecycleFinalFormatResponseError,
                    upstream_empty_completed=(
                        UpstreamEmptyCompletedResponseError
                    ),
                ),
                set_active_prepared_exchange=set_active_exchange,
                activate_attempt=activate_route_attempt,
                safe_json_mapping=_safe_json_mapping,
                official_mutation=official_passthrough_request_body,
                transparent_mutation=transparent_request_body,
                rewrite_developer_roles=rewrite_developer_roles,
                normalize_tool_schema_booleans=(
                    normalize_tool_schema_booleans
                ),
                validate_transparent_tool_loop=(
                    validate_transparent_tool_loop
                ),
                compatibility_mutation=compatible_request_body,
                request_observability=request_observability_for_attempt,
                emit_request_start=emit_request_start_once,
                build_request=build_attempt_request,
                lifecycle_guidance=lifecycle_guidance,
                open_response=open_exchange,
                relay_response=relay_exchange,
                set_upstream_format=set_upstream_format,
                attach_upstream=attach_upstream,
                downstream_exposed=lambda: _downstream_has_been_exposed(
                    self
                ),
                raise_if_cancelled=raise_if_cancelled,
                emit_downstream_retry=emit_downstream_retry,
                finish_downstream_failure=finish_downstream_write_failure,
                failure_class=_upstream_failure_class,
                retry_safety_class=_retry_safety_class,
                model_access_path=_model_access_path_from_event_context,
                retry_after_seconds=_retry_after_delay_seconds,
                emit_retry=_emit_upstream_retry_event,
                emit_retry_suppressed=(
                    _emit_upstream_retry_suppressed_event
                ),
                downstream_retry_payload=_downstream_retry_payload,
                retry_identity=_retry_identity_from_context,
                sleep=_sleep_for_retry_with_gateway_cancellation,
                protocol_fallback=protocol_fallback,
                error_status=exchange_error_status,
                handle_empty_completed=handle_empty_completed,
                monotonic=time.monotonic,
                suppressed_retry_safety_classes=(
                    _SUPPRESSED_RETRY_SAFETY_CLASSES
                ),
                runtime_attempt_key=(
                    _RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY
                ),
            )
            try:
                exchange_result = execute_exchange(
                    exchange_request,
                    exchange_hooks,
                    progress=exchange_progress,
                )
            finally:
                active_route_attempt = exchange_progress.active_attempt
                relay_execution_plan = (
                    exchange_progress.relay_execution_plan
                )
                upstream_format = exchange_progress.upstream_format
                request_observability = (
                    exchange_progress.request_observability
                )
                downstream_sse_started = (
                    exchange_progress.downstream_sse_started
                    or downstream_sse_started
                )
                self._active_prepared_exchange = (
                    exchange_progress.active_prepared_exchange
                )
            terminal = terminal_result(exchange_result)
            if terminal.handled and not terminal.completed:
                return
            if not terminal.completed:
                raise RuntimeError(
                    terminal.error or "invalid exchange terminal result"
                )
            status = terminal.status
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
            control_path = self.path[3:] if self.path.startswith("/v1/") else self.path
            request = _gateway_transport().build_request(
                upstream,
                control_path,
                headers=headers,
                method=method,
            )
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
        return relay_raw_response(
            response,
            upstream_name,
            writer=self,
            filtered_headers=_filtered_response_headers,
            active_request=_active_gateway_request,
            write_body=self._write_non_streaming_body_relay,
        )

    def _send_sse_headers(self, status: int, upstream_name: str) -> bool:
        seam = _handler_downstream_stream_commit(self)
        commit_headers = None if seam is None else seam.commit_headers
        return send_sse_headers(
            self,
            status,
            upstream_name,
            commit_headers=commit_headers,
        )

    def _write_sse_bytes(self, data: bytes, *, observe: bool = True) -> bool:
        seam = _handler_downstream_stream_commit(self)
        commit_sse_bytes = None if seam is None else seam.commit_sse_bytes
        return write_sse_bytes(
            self,
            data,
            commit_sse_bytes=commit_sse_bytes,
            observe=observe,
        )

    def _write_non_streaming_body_relay(self, body: bytes) -> bool:
        return write_non_streaming_body(self, body)

    def _write_sse_event(self, event: str, payload: Mapping[str, Any]) -> bool:
        return write_sse_event(
            self,
            event,
            payload,
            encode_json_line=_sse_json_line,
            commit_sse_bytes=self._write_sse_bytes,
        )

    def _write_sse_data(self, payload: Mapping[str, Any]) -> bool:
        return write_sse_data(
            self,
            payload,
            encode_json_line=_sse_json_line,
            commit_sse_bytes=self._write_sse_bytes,
        )

    def _write_sse_keepalive(self) -> bool:
        return write_sse_keepalive(
            self,
            commit_sse_bytes=self._write_sse_bytes,
        )

    def _write_sse_done(self) -> bool:
        seam = _handler_downstream_stream_commit(self)
        return write_sse_done(
            self,
            commit_sse_bytes=self._write_sse_bytes,
            terminal_committed=seam is not None and seam.terminal_committed,
        )

    def _iter_upstream_sse_lines(
        self,
        response: Any,
        *,
        downstream_output_started: Callable[[], bool] | None = None,
        line_resets_idle_timeout: Callable[[bytes], bool] | None = None,
        on_line: Callable[[bytes], None] | None = None,
    ) -> Any:
        admission = _active_gateway_request()
        seam = _handler_downstream_stream_commit(self)

        def attach_upstream(lifecycle: Any) -> None:
            if seam is not None:
                seam.attach_upstream(lifecycle)

        context = SseLineRelayContext(
            admission=admission,
            keepalive_interval=sse_keepalive_seconds(),
            transport_timeout_seconds=transport_sse_idle_timeout_seconds(),
            model_event_timeout_seconds=model_event_sse_idle_timeout_seconds(),
            lifecycle_factory=lambda upstream_response, current_admission: _UpstreamSseReaderLifecycle(
                upstream_response,
                admission=current_admission,
                logger_hook=logger,
            ),
            attach_upstream=attach_upstream,
            write_keepalive=self._write_sse_keepalive,
            idle_timeout_error=lambda timeout_seconds, phase: UpstreamStreamIdleTimeoutError(
                timeout_seconds,
                phase=phase,
            ),
            keepalive_failure_error=DownstreamKeepaliveFailedError,
            join_timeout_seconds=_UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS,
        )
        return iter_upstream_sse_lines(
            response,
            context=context,
            downstream_output_started=downstream_output_started,
            line_resets_idle_timeout=line_resets_idle_timeout,
            on_line=on_line,
        )

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

    # Stage-1 follow-up: transparent and official relay implementations remain
    # handler callbacks behind this seam; extract those methods in the next sub-slice.
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
            request_scoped_seam.set_output_observer(
                lambda event: _responses_event_commits_downstream_output(event, "")
            )
            request_scoped_seam.set_synthetic_terminal_failure_callback(
                _bind_handler_synthetic_terminal_failure(self, _responses_synthetic_terminal_failure)
            )
            request_scoped_seam.set_usage_line_callback(_offer_official_passthrough_usage_line)
            seam = request_scoped_seam
        else:
            seam = _bind_downstream_stream_commit(
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
                failure_phase=seam.close_phase,
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
            logger_hook=logger,
        )
        seam.attach_upstream(lifecycle)
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

        request_scoped_seam = _handler_downstream_stream_commit(self)
        synthetic_terminal_failure_callback = (
            None
            if chat_mode
            else _responses_synthetic_terminal_failure
        )
        if request_scoped_seam is not None:
            request_scoped_seam.set_terminal_observer(
                _chat_terminal_observer if chat_mode else _responses_terminal_observer
            )
            request_scoped_seam.set_output_observer(
                None
                if chat_mode
                else (lambda event: _responses_event_commits_downstream_output(event, ""))
            )
            request_scoped_seam.set_usage_line_callback(
                lambda context, line: _offer_usage_observed_sse_line(
                    context, line, upstream_format=upstream_format
                )
            )
            request_scoped_seam.set_synthetic_terminal_failure_callback(
                _bind_handler_synthetic_terminal_failure(
                    self,
                    synthetic_terminal_failure_callback,
                    redact_identity=relay_redact_identity,
                )
            )
            seam = request_scoped_seam
        else:
            seam = _bind_downstream_stream_commit(
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
                output_observer=(
                    None
                    if chat_mode
                    else (lambda event: _responses_event_commits_downstream_output(event, ""))
                ),
                usage_line_callback=lambda context, line: _offer_usage_observed_sse_line(
                    context, line, upstream_format=upstream_format
                ),
                synthetic_terminal_failure_callback=synthetic_terminal_failure_callback,
                redact_identity=relay_redact_identity,
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
            logger_hook=logger,
        )
        seam.attach_upstream(lifecycle)
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
        """Adapt the handler to the extracted upstream relay state machine."""
        return relay_upstream_response(
            RelayContext(
                handler=self,
                symbols=_relay_symbols(),
                transparent_relay=self._relay_transparent_upstream_response,
                official_passthrough_relay=self._relay_official_passthrough_sse_response,
                prepared_exchange=getattr(self, "_active_prepared_exchange", None),
            ),
            response,
            upstream_name,
            relay_execution_plan,
            request_id=request_id,
            model=model,
            inbound_format=inbound_format,
            caller_stream=caller_stream,
            event_context=event_context,
            usage_capture=usage_capture,
            headers_already_sent=headers_already_sent,
            defer_stream_errors=defer_stream_errors,
            mark_downstream_sse_started=mark_downstream_sse_started,
            response_lifecycle_state=response_lifecycle_state,
        )

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
