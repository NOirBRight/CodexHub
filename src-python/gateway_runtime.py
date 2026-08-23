from __future__ import annotations

"""Gateway request helpers loaded into the ``codex_proxy`` process-entry dict.

This file is executed by ``codex_proxy`` (not imported as a live singleton) so
tests can patch ``codex_proxy.<name>`` and the handler still sees those names.
Do not ``import gateway_runtime`` from production code or request-path tests:
that would create a second event writer for the same sink.
"""

import sys

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import contextvars
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
_vendored_urllib3 = str(VENDORED_URLLIB3_WHEEL)
if _vendored_urllib3 not in sys.path:
    sys.path.insert(0, _vendored_urllib3)

import urllib3

import gateway_catalog_runtime as _catalog_mod
from gateway_catalog_runtime import CatalogFacts, CatalogRuntime

_OWNED_GENERATED_CATALOG_BY_SLUG = _catalog_mod.generated_catalog_by_slug
_OWNED_GENERATED_CATALOG_SLUGS = _catalog_mod.generated_catalog_slugs
_OWNED_PUBLISHED_CATALOG_MODEL = _catalog_mod.published_catalog_model
_OWNED_CHOOSE_UPSTREAM = _catalog_mod.choose_upstream
_OWNED_OFFICIAL_UPSTREAM = _catalog_mod.official_upstream
_OWNED_CURRENT_CATALOG_DATA = _catalog_mod.current_catalog_data
_OWNED_CATALOG_MAX_OUTPUT_TOKENS = _catalog_mod.catalog_max_output_tokens
_OWNED_OLLAMA_CLOUD_RUNTIME = _catalog_mod.ollama_cloud_runtime_upstream
_OWNED_OLLAMA_CLOUD_ALIAS = _catalog_mod.ollama_cloud_alias_upstream_model
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

from gateway_admission import (
    GATEWAY_SHUTDOWN_CONTROLLER,
    GATEWAY_USER_REQUESTED_SHUTDOWN_BUDGET_SECONDS,
    USER_REQUESTED_SHUTDOWN_OUTCOME,
    GatewayRequestAdmission,
    GatewayShutdownController,
    GatewayUserRequestedShutdown,
    activate_gateway_request as _activate_gateway_request,
    active_gateway_request as _active_gateway_request,
    gateway_shutdown_controller_for_handler as _gateway_shutdown_controller_for_handler,
    restore_gateway_request as _restore_gateway_request,
    sleep_for_retry_with_gateway_cancellation as _sleep_for_retry_with_gateway_cancellation,
)
import gateway_events

import vision_proxy
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
    GatewayChatToResponsesStreamConverter as _ChatToResponsesStreamConverter,
    GatewayResponsesToChatStreamConverter as _ResponsesToChatStreamConverter,
    NonForwardable,
    PreparedExchange,
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
    return gateway_transport.official_proxy_url(url)


def _official_pool_manager(url: str) -> Any:
    return gateway_transport.official_pool_manager(url)


def _official_urlopen(request: Request, *, timeout: float) -> Any:
    return gateway_transport.official_urlopen(request, timeout=timeout)


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
PROXY_TEXT_LOG_PATH = RUNTIME_PROXY_DIR / "codex-proxy.log"

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
    gateway_events.record_user_requested_shutdown()


PROXY_EVENT_LOG_PATH = gateway_events.PROXY_EVENT_LOG_PATH
GATEWAY_EVENT_QUEUE_MAX_RECORDS = gateway_events.GATEWAY_EVENT_QUEUE_MAX_RECORDS
GATEWAY_EVENT_QUEUE_MAX_BYTES = gateway_events.GATEWAY_EVENT_QUEUE_MAX_BYTES
GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS = gateway_events.GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS
gateway_events.refresh_runtime_paths()
GATEWAY_EVENT_WRITER = gateway_events.GATEWAY_EVENT_WRITER
PROXY_EVENT_LOG_PATH = gateway_events.PROXY_EVENT_LOG_PATH
# The compile-selected debug flavor will install a recorder in the later
# Tauri/runtime slice. Normal builds intentionally have no recorder object,
# settings toggle, or environment switch that could activate persistence.
GATEWAY_DIAGNOSTIC_RECORDER = gateway_events.GATEWAY_DIAGNOSTIC_RECORDER
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
    gateway_events.observe_gateway_diagnostic(method, *args, **kwargs)


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
    gateway_events.write_proxy_event(event, **fields)



def _forward_planning_event(event: str, **fields: Any) -> None:
    write_proxy_event(event, **fields)


_route_plan_module._planning_event_sink = _forward_planning_event

def _enqueue_gateway_event_payload(payload: Mapping[str, Any]) -> bool:
    return gateway_events.enqueue_gateway_event_payload(payload)


def flush_proxy_event_writer(timeout: float = 5.0) -> bool:
    return gateway_events.flush_proxy_event_writer(timeout)


def _normalize_usage_for_event(
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str = "upstream_missing_usage",
) -> dict[str, Any]:
    return gateway_events.normalize_usage_for_event(usage, missing_reason=missing_reason)


def _capture_usage(
    usage_capture: dict[str, Any] | None,
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str = "upstream_missing_usage",
) -> None:
    gateway_events.capture_usage(usage_capture, usage, missing_reason=missing_reason)


def _public_event_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    return gateway_events.public_event_context(context)


def _bounded_failure_event_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    return gateway_events.bounded_failure_event_context(context)


def _route_failure_event_fields(context: Mapping[str, Any] | None) -> dict[str, Any]:
    return gateway_events.route_failure_event_fields(context)


def _usage_observed_context(
    event_context: Mapping[str, Any] | None,
    *,
    request_id: str | None,
    model: str | None,
    upstream: str,
    upstream_format: str,
    inbound_format: str,
) -> dict[str, Any]:
    return gateway_events._usage_observed_context(
        event_context,
        request_id=request_id,
        model=model,
        upstream=upstream,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
    )


def _write_usage_observed_event(
    context: Mapping[str, Any],
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str | None = None,
) -> None:
    gateway_events.write_usage_observed_event(context, usage, missing_reason=missing_reason)


def _write_usage_observed_body_event(context: Mapping[str, Any], body: bytes) -> None:
    gateway_events.write_usage_observed_body_event(context, body)


_usage_from_payload = gateway_events._usage_from_payload
_usage_from_json_body = gateway_events._usage_from_json_body
_usage_from_response_event = gateway_events._usage_from_response_event
_usage_int = gateway_events._usage_int
_usage_nested_int = gateway_events._usage_nested_int


OFFICIAL_PASSTHROUGH_USAGE_QUEUE = gateway_events.OFFICIAL_PASSTHROUGH_USAGE_QUEUE
USAGE_OBSERVED_QUEUE = gateway_events.USAGE_OBSERVED_QUEUE


def _offer_official_passthrough_usage_line(context: Mapping[str, Any], line: bytes) -> None:
    gateway_events.offer_official_passthrough_usage_line(context, line)


def _offer_usage_observed_body(context: Mapping[str, Any], body: bytes) -> None:
    gateway_events.offer_usage_observed_body(context, body)


def _offer_usage_observed_sse_line(
    context: Mapping[str, Any],
    line: bytes,
    *,
    upstream_format: str,
) -> None:
    gateway_events.offer_usage_observed_sse_line(context, line, upstream_format=upstream_format)


def _write_adapter_event(event_context: Mapping[str, Any] | None, event: str, **fields: Any) -> None:
    gateway_events.write_adapter_event(event_context, event, **fields)


def _write_failure_event(
    event_context: Mapping[str, Any] | None,
    event: str,
    **fields: Any,
) -> None:
    gateway_events.write_failure_event(event_context, event, **fields)


def _collaboration_adapter() -> CollaborationAdapter:
    """Build a request-time adapter so emit and signing-root patches stay live."""
    return CollaborationAdapter(
        facts=CollaborationFacts(signing_root=WORKER_BINDING_SIGNING_ROOT),
        emit=gateway_events.write_proxy_event,
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
            cache_lookup_override=_vision_proxy_override(
                _owning(vision_proxy, "image_proxy_cache_lookup", _image_proxy_cache_lookup),
                _VISION_ORIGINAL_CACHE_LOOKUP,
            ),
            cache_store_override=_vision_proxy_override(_image_proxy_cache_store, _VISION_ORIGINAL_CACHE_STORE),
            response_body_override=_vision_proxy_override(_image_proxy_response_body, _VISION_ORIGINAL_RESPONSE_BODY),
            response_text_override=_vision_proxy_override(_extract_model_response_text, _VISION_ORIGINAL_EXTRACT_TEXT),
            describe_image_override=_vision_proxy_override(_call_vision_model_for_image_description, _VISION_ORIGINAL_DESCRIBE_IMAGE),
            description_for_part_override=_vision_proxy_override(
                _owning(vision_proxy, "image_proxy_description_for_part", _image_proxy_description_for_part),
                _VISION_ORIGINAL_DESCRIPTION_FOR_PART,
            ),
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


def _owning(module: Any, name: str, fallback: Any) -> Any:
    """Look up a request-time name on an owning module so tests patch that module."""
    current = getattr(module, name, fallback)
    if current is None:
        return fallback
    return current if current is not fallback else fallback


def _catalog_patched(name: str, original: Any) -> Any | None:
    current = getattr(_catalog_mod, name, original)
    if current is original:
        return None
    return current


def _catalog_runtime() -> CatalogRuntime:
    """Build a request-time catalog seam so owning-module monkeypatches stay live."""

    patched_by_slug = _catalog_patched("generated_catalog_by_slug", _OWNED_GENERATED_CATALOG_BY_SLUG)
    patched_published = _catalog_patched("published_catalog_model", _OWNED_PUBLISHED_CATALOG_MODEL)
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
        catalog_path_reader=_owning(_catalog_mod, "existing_generated_catalog_path", existing_generated_catalog_path),
        catalog_models_reader=load_catalog_models,
        policy_reader=_owning(_catalog_mod, "load_policy", load_policy),
        routing_config_reader=lambda: load_routing_config(),
        external_model_reader=_owning(_catalog_mod, "resolve_external_model_alias", resolve_external_model_alias),
        ollama_model_reader=_owning(_catalog_mod, "resolve_ollama_cloud_model", resolve_ollama_cloud_model),
        vision_proxy_enabled_reader=gateway_image_proxy_enabled,
        official_base_url_reader=_catalog_override(official_base_url, _CATALOG_ORIGINAL_OFFICIAL_BASE_URL),
        ollama_base_url_reader=_catalog_override(ollama_cloud_base_url, _CATALOG_ORIGINAL_OLLAMA_BASE_URL),
        official_fast_projection_reader=_catalog_override(catalog_with_official_fast_variants, _CATALOG_ORIGINAL_FAST_VARIANTS),
        context_guard_reader=_catalog_override(catalog_with_openai_context_guard, _CATALOG_ORIGINAL_CONTEXT_GUARD),
        vision_projection_reader=_catalog_override(catalog_with_vision_proxy_capabilities, _CATALOG_ORIGINAL_VISION_PROJECTION),
        canonical_models_reader=_catalog_override(canonical_catalog_models, _CATALOG_ORIGINAL_CANONICAL_MODELS),
        modalities_reader=_catalog_override(_modalities_include_image, _CATALOG_ORIGINAL_MODALITIES),
        input_modalities_reader=_catalog_override(_catalog_input_modalities, _CATALOG_ORIGINAL_INPUT_MODALITIES),
        generated_catalog_by_slug_reader=patched_by_slug,
        published_budget_reader=_catalog_override(published_official_context_budgets, _CATALOG_ORIGINAL_PUBLISHED_BUDGETS),
        known_official_ids_reader=catalog_known_official_model_ids,
        official_display_name_reader=official_short_display_name,
        catalog_by_slug_reader=(lambda: patched_by_slug()) if patched_by_slug is not None else None,
        published_model_reader=patched_published,
        generated_official_reader=generated_official_catalog_upstream_model,
        official_alias_reader=official_alias_upstream_model,
        official_fast_variant_reader=official_fast_variant_upstream_model,
        ollama_runtime_reader=_catalog_patched("ollama_cloud_runtime_upstream", _OWNED_OLLAMA_CLOUD_RUNTIME),
        ollama_alias_reader=_catalog_patched("ollama_cloud_alias_upstream_model", _OWNED_OLLAMA_CLOUD_ALIAS),
        should_include_model_reader=_owning(_catalog_mod, "should_include_model", should_include_model),
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
    live = _catalog_patched("generated_catalog_slugs", _OWNED_GENERATED_CATALOG_SLUGS)
    if live is not None:
        return live(path)
    return _catalog_runtime().generated_catalog_slugs(path)


def generated_catalog_by_slug(path: Path = GENERATED_CATALOG_PATH) -> dict[str, dict[str, Any]]:
    live = _catalog_patched("generated_catalog_by_slug", _OWNED_GENERATED_CATALOG_BY_SLUG)
    if live is not None:
        return live(path)
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
    live = _catalog_patched("catalog_max_output_tokens", _OWNED_CATALOG_MAX_OUTPUT_TOKENS)
    if live is not None:
        return live(model_id)
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
    live = _catalog_patched("ollama_cloud_runtime_upstream", _OWNED_OLLAMA_CLOUD_RUNTIME)
    if live is not None:
        return live(model_id, policy)
    return _catalog_runtime().ollama_cloud_runtime_upstream(model_id, policy)


def ollama_cloud_alias_upstream_model(slug: str, policy: Any) -> dict[str, Any] | None:
    live = _catalog_patched("ollama_cloud_alias_upstream_model", _OWNED_OLLAMA_CLOUD_ALIAS)
    if live is not None:
        return live(slug, policy)
    return _catalog_runtime().ollama_cloud_alias_upstream_model(slug, policy)


def _route_capability_metadata(source: Mapping[str, Any]) -> dict[str, Any]:
    return CatalogRuntime.route_capability_metadata(source)


def choose_upstream(model_id: str) -> dict[str, Any]:
    live = _catalog_patched("choose_upstream", _OWNED_CHOOSE_UPSTREAM)
    if live is not None:
        return live(model_id)
    return _catalog_runtime().choose_upstream(model_id)


def official_upstream() -> dict[str, Any]:
    live = _catalog_patched("official_upstream", _OWNED_OFFICIAL_UPSTREAM)
    if live is not None:
        return live()
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
    relay_official_passthrough_sse_response,
    relay_raw_response,
    relay_transparent_upstream_response,
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
    payload, _ = normalize_third_party_tool_call(payload)
    payload, _ = downgrade_invalid_third_party_tool_calls(payload)
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


import gateway_compat
from gateway_compat import host as _gateway_compat_host
_gateway_compat_host.bind(globals())

def _compat_forward(name: str):
    def _forward(*args, **kwargs):
        return getattr(gateway_compat, name)(*args, **kwargs)
    _forward.__name__ = name
    _forward.__qualname__ = name
    return _forward

def _compat_bind_name(name: str):
    value = gateway_compat.lookup(name)
    if isinstance(value, type) or not callable(value):
        return value
    return _compat_forward(name)

LIFECYCLE_FINAL_RETRY_GUIDANCE = _compat_bind_name('LIFECYCLE_FINAL_RETRY_GUIDANCE')
STRICT_APPLY_PATCH_CUSTOM_TOOL_FIELDS = _compat_bind_name('STRICT_APPLY_PATCH_CUSTOM_TOOL_FIELDS')
STRICT_APPLY_PATCH_EXAMPLE = _compat_bind_name('STRICT_APPLY_PATCH_EXAMPLE')
STRICT_APPLY_PATCH_FORMAT_FIELDS = _compat_bind_name('STRICT_APPLY_PATCH_FORMAT_FIELDS')
WORKER_SUBAGENT_FINALIZATION_GUIDANCE = _compat_bind_name('WORKER_SUBAGENT_FINALIZATION_GUIDANCE')
_RUNTIME_TOOL_CAPABILITY_MANIFEST_ERROR_CODE = _compat_bind_name('_RUNTIME_TOOL_CAPABILITY_MANIFEST_ERROR_CODE')
_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY = _compat_bind_name('_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY')
_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY = _compat_bind_name('_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY')
_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY = _compat_bind_name('_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY')
_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY = _compat_bind_name('_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY')
_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY = _compat_bind_name('_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY')
_TOOL_SCHEMA_LIST_KEYS = _compat_bind_name('_TOOL_SCHEMA_LIST_KEYS')
_TOOL_SCHEMA_MAP_KEYS = _compat_bind_name('_TOOL_SCHEMA_MAP_KEYS')
_TOOL_SCHEMA_VALUE_KEYS = _compat_bind_name('_TOOL_SCHEMA_VALUE_KEYS')
_ThirdPartyApplyPatchStreamAdapter = _compat_bind_name('_ThirdPartyApplyPatchStreamAdapter')
_active_user_request_text = _compat_bind_name('_active_user_request_text')
_adapt_apply_patch_custom_tool_history = _compat_bind_name('_adapt_apply_patch_custom_tool_history')
_adapt_native_responses_tool_declarations = _compat_bind_name('_adapt_native_responses_tool_declarations')
_adapt_third_party_apply_patch_response_body = _compat_bind_name('_adapt_third_party_apply_patch_response_body')
_adapt_third_party_apply_patch_stream_events = _compat_bind_name('_adapt_third_party_apply_patch_stream_events')
_append_internal_field = _compat_bind_name('_append_internal_field')
_apply_external_worker_response_contract = _compat_bind_name('_apply_external_worker_response_contract')
_apply_ollama_reasoning_effort_alias = _compat_bind_name('_apply_ollama_reasoning_effort_alias')
_apply_patch_adapter_enabled = _compat_bind_name('_apply_patch_adapter_enabled')
_apply_runtime_tool_compatibility_plan = _compat_bind_name('_apply_runtime_tool_compatibility_plan')
_assistant_transcript_message = _compat_bind_name('_assistant_transcript_message')
_attach_worker_requested_binding_sidecars = _compat_bind_name('_attach_worker_requested_binding_sidecars')
_bounded_empty_tool_search_terminal_calls = _compat_bind_name('_bounded_empty_tool_search_terminal_calls')
_bounded_tool_search_query_digests = _compat_bind_name('_bounded_tool_search_query_digests')
_bounded_tool_search_unavailable_message = _compat_bind_name('_bounded_tool_search_unavailable_message')
_closed_multi_agent_ids = _compat_bind_name('_closed_multi_agent_ids')
_codex_apps_flat_alias_name = _compat_bind_name('_codex_apps_flat_alias_name')
_codex_apps_flat_alias_parts = _compat_bind_name('_codex_apps_flat_alias_parts')
_codex_apps_namespace_flat_alias = _compat_bind_name('_codex_apps_namespace_flat_alias')
_coerce_exact_spawn_prompt_tool_calls = _compat_bind_name('_coerce_exact_spawn_prompt_tool_calls')
_coerce_exact_spawn_prompt_tool_calls_inner = _compat_bind_name('_coerce_exact_spawn_prompt_tool_calls_inner')
_coerce_required_subagent_tool_calls = _compat_bind_name('_coerce_required_subagent_tool_calls')
_coerce_required_subagent_tool_calls_inner = _compat_bind_name('_coerce_required_subagent_tool_calls_inner')
_compatible_compaction_message = _compat_bind_name('_compatible_compaction_message')
_compatible_internal_message = _compat_bind_name('_compatible_internal_message')
_compatible_multi_agent_call_message = _compat_bind_name('_compatible_multi_agent_call_message')
_compatible_multi_agent_output_message = _compat_bind_name('_compatible_multi_agent_output_message')
_compatible_node_repl_call_message = _compat_bind_name('_compatible_node_repl_call_message')
_compatible_node_repl_output_message = _compat_bind_name('_compatible_node_repl_output_message')
_compatible_tool_message = _compat_bind_name('_compatible_tool_message')
_completed_multi_agent_wait_ids = _compat_bind_name('_completed_multi_agent_wait_ids')
_contains_response_function_call = _compat_bind_name('_contains_response_function_call')
_coordinator_forbidden_tool_suppressed_message = _compat_bind_name('_coordinator_forbidden_tool_suppressed_message')
_deferred_namespace_surface_counts = _compat_bind_name('_deferred_namespace_surface_counts')
_developer_text_message = _compat_bind_name('_developer_text_message')
_downgrade_invalid_third_party_tool_calls = _compat_bind_name('_downgrade_invalid_third_party_tool_calls')
_drop_chat_message_phase = _compat_bind_name('_drop_chat_message_phase')
_drop_v2_chat_reasoning_history = _compat_bind_name('_drop_v2_chat_reasoning_history')
_dump_arguments_like = _compat_bind_name('_dump_arguments_like')
_exact_child_prompts_from_request_text = _compat_bind_name('_exact_child_prompts_from_request_text')
_excessive_transparent_chat_tool_loop_count = _compat_bind_name('_excessive_transparent_chat_tool_loop_count')
_excessive_transparent_responses_tool_loop_count = _compat_bind_name('_excessive_transparent_responses_tool_loop_count')
_explicit_function_tool = _compat_bind_name('_explicit_function_tool')
_filter_tools_for_subagent_coordinator = _compat_bind_name('_filter_tools_for_subagent_coordinator')
_filter_tools_for_subagent_worker = _compat_bind_name('_filter_tools_for_subagent_worker')
_flatten_namespace_function_tools = _compat_bind_name('_flatten_namespace_function_tools')
_function_call_namespace = _compat_bind_name('_function_call_namespace')
_function_tool_names = _compat_bind_name('_function_tool_names')
_guard_duplicate_multi_agent_spawn_calls = _compat_bind_name('_guard_duplicate_multi_agent_spawn_calls')
_guard_duplicate_multi_agent_spawn_calls_inner = _compat_bind_name('_guard_duplicate_multi_agent_spawn_calls_inner')
_has_completed_single_loop_multi_agent_context = _compat_bind_name('_has_completed_single_loop_multi_agent_context')
_has_completed_single_step_node_repl_context = _compat_bind_name('_has_completed_single_step_node_repl_context')
_has_invalid_tool_name = _compat_bind_name('_has_invalid_tool_name')
_has_multi_agent_discovery_context = _compat_bind_name('_has_multi_agent_discovery_context')
_has_multi_agent_discovery_tools = _compat_bind_name('_has_multi_agent_discovery_tools')
_has_node_repl_subagent_plan_read_context = _compat_bind_name('_has_node_repl_subagent_plan_read_context')
_has_open_multi_agent_context = _compat_bind_name('_has_open_multi_agent_context')
_has_single_loop_multi_agent_request = _compat_bind_name('_has_single_loop_multi_agent_request')
_has_single_step_node_repl_request = _compat_bind_name('_has_single_step_node_repl_request')
_has_worker_subagent_finalization_guidance = _compat_bind_name('_has_worker_subagent_finalization_guidance')
_hide_tools_for_completed_subagent_lifecycle = _compat_bind_name('_hide_tools_for_completed_subagent_lifecycle')
_hoist_additional_tools_input_items = _compat_bind_name('_hoist_additional_tools_input_items')
_inject_explicit_codex_tools = _compat_bind_name('_inject_explicit_codex_tools')
_is_flattened_namespace_schema = _compat_bind_name('_is_flattened_namespace_schema')
_is_legacy_native_worker_spawn_call = _compat_bind_name('_is_legacy_native_worker_spawn_call')
_is_legacy_native_worker_spawn_readback = _compat_bind_name('_is_legacy_native_worker_spawn_readback')
_is_local_tool_gateway_tool_schema = _compat_bind_name('_is_local_tool_gateway_tool_schema')
_is_mcp_or_codex_app_function_call = _compat_bind_name('_is_mcp_or_codex_app_function_call')
_is_mcp_or_codex_app_tool_schema = _compat_bind_name('_is_mcp_or_codex_app_tool_schema')
_is_multi_agent_discovery_arguments = _compat_bind_name('_is_multi_agent_discovery_arguments')
_is_multi_agent_explicit_tool_name = _compat_bind_name('_is_multi_agent_explicit_tool_name')
_is_multi_agent_namespace_name = _compat_bind_name('_is_multi_agent_namespace_name')
_is_multi_agent_spawn_function_call = _compat_bind_name('_is_multi_agent_spawn_function_call')
_is_multi_agent_tool_schema = _compat_bind_name('_is_multi_agent_tool_schema')
_is_node_repl_explicit_tool_name = _compat_bind_name('_is_node_repl_explicit_tool_name')
_is_node_repl_tool_schema = _compat_bind_name('_is_node_repl_tool_schema')
_is_raw_namespace_schema = _compat_bind_name('_is_raw_namespace_schema')
_is_raw_provider_probe_context = _compat_bind_name('_is_raw_provider_probe_context')
_is_standard_responses_function_call = _compat_bind_name('_is_standard_responses_function_call')
_is_tool_call_item = _compat_bind_name('_is_tool_call_item')
_joined_text = _compat_bind_name('_joined_text')
_json_argument_string_needs_repair = _compat_bind_name('_json_argument_string_needs_repair')
_json_object_from_arguments = _compat_bind_name('_json_object_from_arguments')
_lifecycle_final_retry_guidance_message = _compat_bind_name('_lifecycle_final_retry_guidance_message')
_line_value = _compat_bind_name('_line_value')
_looks_like_coordinator_local_function_call = _compat_bind_name('_looks_like_coordinator_local_function_call')
_looks_like_response_tool_name_fragment = _compat_bind_name('_looks_like_response_tool_name_fragment')
_looks_like_subagent_workflow_plan_text = _compat_bind_name('_looks_like_subagent_workflow_plan_text')
_looks_like_unknown_multi_agent_function_call = _compat_bind_name('_looks_like_unknown_multi_agent_function_call')
_mark_lifecycle_final_seen_if_present = _compat_bind_name('_mark_lifecycle_final_seen_if_present')
_message_item_visible_text = _compat_bind_name('_message_item_visible_text')
_multi_agent_alias_tool_name = _compat_bind_name('_multi_agent_alias_tool_name')
_multi_agent_current_state_message = _compat_bind_name('_multi_agent_current_state_message')
_multi_agent_discovery_arguments = _compat_bind_name('_multi_agent_discovery_arguments')
_multi_agent_discovery_output_item = _compat_bind_name('_multi_agent_discovery_output_item')
_multi_agent_explicit_function_tools = _compat_bind_name('_multi_agent_explicit_function_tools')
_multi_agent_function_call_name = _compat_bind_name('_multi_agent_function_call_name')
_multi_agent_lifecycle_complete_message = _compat_bind_name('_multi_agent_lifecycle_complete_message')
_multi_agent_result_text = _compat_bind_name('_multi_agent_result_text')
_multi_agent_spawn_more_message = _compat_bind_name('_multi_agent_spawn_more_message')
_node_repl_function_call_name = _compat_bind_name('_node_repl_function_call_name')
_node_repl_single_step_complete_message = _compat_bind_name('_node_repl_single_step_complete_message')
_normalize_multi_agent_arguments = _compat_bind_name('_normalize_multi_agent_arguments')
_normalize_third_party_tool_call = _compat_bind_name('_normalize_third_party_tool_call')
_normalize_tool_json_schema = _compat_bind_name('_normalize_tool_json_schema')
_normalize_tool_json_schema_items = _compat_bind_name('_normalize_tool_json_schema_items')
_normalize_tool_search_arguments = _compat_bind_name('_normalize_tool_search_arguments')
_normalize_transparent_tool_schema_booleans = _compat_bind_name('_normalize_transparent_tool_schema_booleans')
_open_multi_agent_ids = _compat_bind_name('_open_multi_agent_ids')
_post_final_multi_agent_suppressed_item_id = _compat_bind_name('_post_final_multi_agent_suppressed_item_id')
_prepare_runtime_tool_compatibility = _compat_bind_name('_prepare_runtime_tool_compatibility')
_raise_malformed_runtime_tool_capability_manifest = _compat_bind_name('_raise_malformed_runtime_tool_capability_manifest')
_raise_native_responses_tool_contract_error = _compat_bind_name('_raise_native_responses_tool_contract_error')
_raise_on_invalid_worker_stream_event = _compat_bind_name('_raise_on_invalid_worker_stream_event')
_raise_runtime_tool_compatibility_error = _compat_bind_name('_raise_runtime_tool_compatibility_error')
_raise_worker_contract_error = _compat_bind_name('_raise_worker_contract_error')
_reconcile_function_call_argument_events = _compat_bind_name('_reconcile_function_call_argument_events')
_reject_missing_worker_selector_for_generated_call = _compat_bind_name('_reject_missing_worker_selector_for_generated_call')
_remember_worker_stream_event = _compat_bind_name('_remember_worker_stream_event')
_remember_worker_stream_item = _compat_bind_name('_remember_worker_stream_item')
_repair_missing_required_subagent_call_events = _compat_bind_name('_repair_missing_required_subagent_call_events')
_repair_missing_required_subagent_call_payload = _compat_bind_name('_repair_missing_required_subagent_call_payload')
_repair_missing_required_subagent_call_sse_line = _compat_bind_name('_repair_missing_required_subagent_call_sse_line')
_replace_embedded_model = _compat_bind_name('_replace_embedded_model')
_requested_multi_agent_spawn_count = _compat_bind_name('_requested_multi_agent_spawn_count')
_requested_reasoning_effort = _compat_bind_name('_requested_reasoning_effort')
_requested_worker_binding_signature = _compat_bind_name('_requested_worker_binding_signature')
_required_spawn_arguments_for_state = _compat_bind_name('_required_spawn_arguments_for_state')
_required_subagent_call_events = _compat_bind_name('_required_subagent_call_events')
_required_subagent_call_item = _compat_bind_name('_required_subagent_call_item')
_required_subagent_call_item_like = _compat_bind_name('_required_subagent_call_item_like')
_required_subagent_call_spec = _compat_bind_name('_required_subagent_call_spec')
_required_subagent_send_input_message = _compat_bind_name('_required_subagent_send_input_message')
_required_subagent_tool_choice = _compat_bind_name('_required_subagent_tool_choice')
_required_workflow_spawn_arguments = _compat_bind_name('_required_workflow_spawn_arguments')
_response_events_are_text_or_empty = _compat_bind_name('_response_events_are_text_or_empty')
_response_output_is_text_or_empty = _compat_bind_name('_response_output_is_text_or_empty')
_responses_body_with_lifecycle_final_retry_guidance = _compat_bind_name('_responses_body_with_lifecycle_final_retry_guidance')
_restore_deferred_core_node_repl_namespace = _compat_bind_name('_restore_deferred_core_node_repl_namespace')
_restrict_bounded_tool_search_queries = _compat_bind_name('_restrict_bounded_tool_search_queries')
_restrict_tools_to_required_tool = _compat_bind_name('_restrict_tools_to_required_tool')
_rewrite_generated_guidance_tool_name = _compat_bind_name('_rewrite_generated_guidance_tool_name')
_rewrite_internal_input_items = _compat_bind_name('_rewrite_internal_input_items')
_rewrite_structured_tool_input_items = _compat_bind_name('_rewrite_structured_tool_input_items')
_rewrite_transparent_developer_role_messages = _compat_bind_name('_rewrite_transparent_developer_role_messages')
_rewrite_v2_unsupported_tool_history = _compat_bind_name('_rewrite_v2_unsupported_tool_history')
_runtime_alias_for_namespace_child = _compat_bind_name('_runtime_alias_for_namespace_child')
_runtime_alias_matches_namespace = _compat_bind_name('_runtime_alias_matches_namespace')
_runtime_plan_has_native_plain_function = _compat_bind_name('_runtime_plan_has_native_plain_function')
_runtime_required_tool_diagnostics = _compat_bind_name('_runtime_required_tool_diagnostics')
_runtime_tool_adapter_alias_hash = _compat_bind_name('_runtime_tool_adapter_alias_hash')
_runtime_tool_adapter_item_snapshot = _compat_bind_name('_runtime_tool_adapter_item_snapshot')
_runtime_tool_adapter_request_snapshot = _compat_bind_name('_runtime_tool_adapter_request_snapshot')
_runtime_tool_alias_token = _compat_bind_name('_runtime_tool_alias_token')
_runtime_tool_compatibility_plan = _compat_bind_name('_runtime_tool_compatibility_plan')
_runtime_tool_compatibility_plan_for_attempt = _compat_bind_name('_runtime_tool_compatibility_plan_for_attempt')
_runtime_tool_compatibility_stream_for_attempt = _compat_bind_name('_runtime_tool_compatibility_stream_for_attempt')
_runtime_tool_protocol_capabilities = _compat_bind_name('_runtime_tool_protocol_capabilities')
_safe_json_mapping = _compat_bind_name('_safe_json_mapping')
_same_selected_v1_collaboration_function_call = _compat_bind_name('_same_selected_v1_collaboration_function_call')
_sanitize_official_invalid_tool_calls = _compat_bind_name('_sanitize_official_invalid_tool_calls')
_sanitize_official_system_messages = _compat_bind_name('_sanitize_official_system_messages')
_sanitize_unsupported_compaction_input_items = _compat_bind_name('_sanitize_unsupported_compaction_input_items')
_set_required_subagent_tool_choice = _compat_bind_name('_set_required_subagent_tool_choice')
_single_line_internal_field = _compat_bind_name('_single_line_internal_field')
_spawned_multi_agent_ids = _compat_bind_name('_spawned_multi_agent_ids')
_split_agent_id_list = _compat_bind_name('_split_agent_id_list')
_split_namespace_tool_alias = _compat_bind_name('_split_namespace_tool_alias')
_status_completed_agent_ids = _compat_bind_name('_status_completed_agent_ids')
_status_not_found_agent_ids = _compat_bind_name('_status_not_found_agent_ids')
_strict_apply_patch_function_tool = _compat_bind_name('_strict_apply_patch_function_tool')
_string_list = _compat_bind_name('_string_list')
_stringify_internal_field = _compat_bind_name('_stringify_internal_field')
_structured_tool_function_call_item = _compat_bind_name('_structured_tool_function_call_item')
_supports_explicit_namespace_alias = _compat_bind_name('_supports_explicit_namespace_alias')
_suppress_bounded_tool_search_calls = _compat_bind_name('_suppress_bounded_tool_search_calls')
_suppress_bounded_tool_search_calls_inner = _compat_bind_name('_suppress_bounded_tool_search_calls_inner')
_suppress_coordinator_forbidden_tool_calls = _compat_bind_name('_suppress_coordinator_forbidden_tool_calls')
_suppress_coordinator_forbidden_tool_calls_inner = _compat_bind_name('_suppress_coordinator_forbidden_tool_calls_inner')
_suppress_multi_agent_calls_after_lifecycle_final = _compat_bind_name('_suppress_multi_agent_calls_after_lifecycle_final')
_suppress_multi_agent_calls_after_lifecycle_final_inner = _compat_bind_name('_suppress_multi_agent_calls_after_lifecycle_final_inner')
_suppress_worker_multi_agent_tool_calls = _compat_bind_name('_suppress_worker_multi_agent_tool_calls')
_suppress_worker_multi_agent_tool_calls_inner = _compat_bind_name('_suppress_worker_multi_agent_tool_calls_inner')
_suppressed_duplicate_spawn_message = _compat_bind_name('_suppressed_duplicate_spawn_message')
_terminalize_bounded_empty_tool_search_misses = _compat_bind_name('_terminalize_bounded_empty_tool_search_misses')
_text_contains_multi_agent_discovery = _compat_bind_name('_text_contains_multi_agent_discovery')
_tool_parameters_schema = _compat_bind_name('_tool_parameters_schema')
_tool_schema_name = _compat_bind_name('_tool_schema_name')
_tool_search_call_arguments = _compat_bind_name('_tool_search_call_arguments')
_tool_search_query_digest = _compat_bind_name('_tool_search_query_digest')
_transcript_text = _compat_bind_name('_transcript_text')
_valid_namespace_function_names = _compat_bind_name('_valid_namespace_function_names')
_valid_tool_name = _compat_bind_name('_valid_tool_name')
_validate_external_worker_selectors = _compat_bind_name('_validate_external_worker_selectors')
_validate_generated_required_spawn_call = _compat_bind_name('_validate_generated_required_spawn_call')
_validate_runtime_tool_capability_facts = _compat_bind_name('_validate_runtime_tool_capability_facts')
_validate_strict_apply_patch_custom_tool = _compat_bind_name('_validate_strict_apply_patch_custom_tool')
_validate_worker_binding_history = _compat_bind_name('_validate_worker_binding_history')
_verified_worker_requested_binding = _compat_bind_name('_verified_worker_requested_binding')
_with_preserved_spawn_agent_type = _compat_bind_name('_with_preserved_spawn_agent_type')
_worker_caller_carrier_supported = _compat_bind_name('_worker_caller_carrier_supported')
_worker_multi_agent_suppressed_message = _compat_bind_name('_worker_multi_agent_suppressed_message')
_worker_requested_binding_sidecar = _compat_bind_name('_worker_requested_binding_sidecar')
_worker_requested_binding_signature_payload = _compat_bind_name('_worker_requested_binding_signature_payload')
_worker_subagent_finalization_message = _compat_bind_name('_worker_subagent_finalization_message')
_workflow_baseline_status = _compat_bind_name('_workflow_baseline_status')
_write_required_subagent_repair_event = _compat_bind_name('_write_required_subagent_repair_event')
_write_runtime_tool_adapter_request_evidence = _compat_bind_name('_write_runtime_tool_adapter_request_evidence')
_write_runtime_tool_adapter_response_evidence = _compat_bind_name('_write_runtime_tool_adapter_response_evidence')
compatible_request_body = _compat_bind_name('compatible_request_body')
compatible_response_body = _compat_bind_name('compatible_response_body')
compatible_sse_line = _compat_bind_name('compatible_sse_line')
official_passthrough_request_body = _compat_bind_name('official_passthrough_request_body')
transparent_request_body = _compat_bind_name('transparent_request_body')


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


def _facade_ns() -> dict[str, Any]:
    """Prefer the process-entry module dict so tests can patch ``codex_proxy`` names."""

    facade = sys.modules.get("codex_proxy")
    if facade is not None and getattr(facade, "__name__", None) == "codex_proxy":
        return vars(facade)
    return globals()


def _live(name: str, fallback: Any) -> Any:
    """Prefer a patched facade binding, including the public name without the underscore."""
    ns = _facade_ns()
    keys = [name]
    if name.startswith("_") and len(name) > 1 and name[1].isalpha():
        keys.append(name[1:])
    elif name and not name.startswith("_"):
        keys.append(f"_{name}")
    for key in keys:
        current = ns.get(key, fallback)
        if current is not fallback:
            return current
    return ns.get(name, fallback)


def _gateway_transport() -> GatewayTransport:
    """Build a request-time adapter so official/urlopen/token/sleep patches stay live."""
    return GatewayTransport(
        facts=TransportFacts(
            hop_by_hop_request_headers=frozenset(_live("HOP_BY_HOP_REQUEST_HEADERS", HOP_BY_HOP_REQUEST_HEADERS)),
            official_alias_prefix=_live("OFFICIAL_ALIAS_PREFIX", OFFICIAL_ALIAS_PREFIX),
            official_responses_lite_unsupported_models=frozenset(
                _live("OFFICIAL_RESPONSES_LITE_UNSUPPORTED_MODELS", OFFICIAL_RESPONSES_LITE_UNSUPPORTED_MODELS)
            ),
            official_passthrough_behavior=_live(
                "BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH",
                BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            ),
            official_upstream_name="official",
            suppressed_retry_safety_classes=_live(
                "_SUPPRESSED_RETRY_SAFETY_CLASSES", _SUPPRESSED_RETRY_SAFETY_CLASSES
            ),
            downstream_closed_before_retry_error=_live(
                "DownstreamClosedBeforeRetryError", DownstreamClosedBeforeRetryError
            ),
        ),
        official_open=None,
        standard_open=gateway_transport.urlopen,
        open_once_hook=None,
        sleep=None,
        active_request=_live("_active_gateway_request", _active_gateway_request),
        access_token=_live("codex_access_token", codex_access_token),
        account_id=_live("codex_account_id", codex_account_id),
        diagnostic_recorder=_owning(gateway_events, "GATEWAY_DIAGNOSTIC_RECORDER", None),
        diagnostic_context_value=_live("_diagnostic_context_value", _diagnostic_context_value),
        diagnostic_connection_disposition=_live(
            "_diagnostic_connection_disposition", _diagnostic_connection_disposition
        ),
        diagnostic_error_connection_disposition=_live(
            "_diagnostic_error_connection_disposition",
            _diagnostic_error_connection_disposition,
        ),
        diagnostic_response_metadata=_live("_diagnostic_response_metadata", _diagnostic_response_metadata),
        diagnostic_transport_phase=_live("_diagnostic_transport_phase", _diagnostic_transport_phase),
        emit_retry=_live("_emit_upstream_retry_event", _emit_upstream_retry_event),
        emit_retry_suppressed=_live(
            "_emit_upstream_retry_suppressed_event", _emit_upstream_retry_suppressed_event
        ),
        retry_delay_seconds=_live("gateway_retry_delay_seconds", gateway_retry_delay_seconds),
        failure_class_hook=_live("_upstream_failure_class", _upstream_failure_class),
        retry_after_hook=_live("_retry_after_delay_seconds", _retry_after_delay_seconds),
        retry_attempts_for_failure_class=_live(
            "_retry_attempts_for_failure_class", _retry_attempts_for_failure_class
        ),
        capacity_elapsed_allows=_live(
            "_capacity_retry_elapsed_limit_allows", _capacity_retry_elapsed_limit_allows
        ),
        retry_safety_class=_live("_retry_safety_class", _retry_safety_class),
        retry_safety_failure_phase=_live("_retry_safety_failure_phase", _retry_safety_failure_phase),
        failure_phase=gateway_transport.transport_failure_phase,
        model_access_path=_live("_model_access_path_from_event_context", _model_access_path_from_event_context),
        model_access_path_idempotent=_live(
            "_model_access_path_idempotency_guaranteed", _model_access_path_idempotency_guaranteed
        ),
        ensure_retry_identity=_live("_ensure_retry_attempt_identity", _ensure_retry_attempt_identity),
        retry_identity_from_context=_live("_retry_identity_from_context", _retry_identity_from_context),
        downstream_retry_payload=_live("_downstream_retry_payload", _downstream_retry_payload),
        get_header=_live("_get_header", _get_header),
        header_items=_live("_header_items", _header_items),
        upstream_retry_attempts=_live("_upstream_retry_attempts", _upstream_retry_attempts),
        getproxies=None,
        getproxies_registry=None,
        proxy_bypass=None,
        platform=sys.platform,
        official_pools=None,
        official_pools_lock=None,
        pool_manager_hook=None,
        proxy_url_hook=None,
        endpoint_url_hook=_live("_upstream_endpoint_url", _upstream_endpoint_url),
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
    live = _live("upstream_headers", None)
    if live is not None and live is not upstream_headers:
        return live(
            incoming_headers,
            upstream,
            drop_content_encoding=drop_content_encoding,
            behavior_profile=behavior_profile,
            model_id=model_id,
            authentication_strategy=authentication_strategy,
            request_mutation_policy=request_mutation_policy,
            operational_authentication=operational_authentication,
        )
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
    live = _catalog_patched("current_catalog_data", _OWNED_CURRENT_CATALOG_DATA)
    if live is not None:
        return live()
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
_CATALOG_HOOK_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("catalog_hook_depth", default=0)


def _catalog_override(candidate: Callable[..., Any], original: Callable[..., Any]) -> Callable[..., Any] | None:
    if candidate is original or _CATALOG_HOOK_DEPTH.get() > 0:
        return None

    def invoke(*args: Any, **kwargs: Any) -> Any:
        token = _CATALOG_HOOK_DEPTH.set(_CATALOG_HOOK_DEPTH.get() + 1)
        try:
            return candidate(*args, **kwargs)
        finally:
            _CATALOG_HOOK_DEPTH.reset(token)

    return invoke


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
    handler: Any,
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


_UpstreamSseReaderLifecycle = UpstreamSseReaderLifecycle


# Build explicit facade bindings per relay request so compatibility patches stay live.
def _relay_write_proxy_event(event: str, **fields: Any) -> None:
    write_proxy_event(event, **fields)

def _relay_compatible_sse_line(*args: Any, **kwargs: Any) -> Any:
    return compatible_sse_line(*args, **kwargs)


def _relay_active_gateway_request() -> Any:
    return _active_gateway_request()


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
            _downgrade_invalid_third_party_tool_calls=gateway_compat.lookup(
                "_downgrade_invalid_third_party_tool_calls"
            ),
            _events_to_responses_body=_events_to_responses_body,
            _filtered_response_headers=_filtered_response_headers,
            _guard_duplicate_multi_agent_spawn_calls=gateway_compat.lookup(
                "_guard_duplicate_multi_agent_spawn_calls"
            ),
            _handler_downstream_stream_commit=_handler_downstream_stream_commit,
            _incomplete_stream_json_error_body=_incomplete_stream_json_error_body,
            _is_event_stream=_is_event_stream,
            _is_reasoning_summary_stream_event=_is_reasoning_summary_stream_event,
            _is_sse_blank_line=_is_sse_blank_line,
            _is_sse_event_metadata_line=_is_sse_event_metadata_line,
            _json_error_payload_for_inbound_format=_json_error_payload_for_inbound_format,
            _lifecycle_final_issue_event_name=_lifecycle_final_issue_event_name,
            _lifecycle_final_issue_missing_reason=_lifecycle_final_issue_missing_reason,
            _normalize_third_party_tool_call=gateway_compat.lookup(
                "_normalize_third_party_tool_call"
            ),
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
            _active_gateway_request=_relay_active_gateway_request,
            _bind_handler_synthetic_terminal_failure=_bind_handler_synthetic_terminal_failure,
            _responses_synthetic_terminal_failure=_responses_synthetic_terminal_failure,
            _offer_official_passthrough_usage_line=_offer_official_passthrough_usage_line,
            _UpstreamSseReaderLifecycle=_UpstreamSseReaderLifecycle,
            logger=logger,
            _UNSET_CONTENT_ENCODING=_UNSET_CONTENT_ENCODING,
            _chat_completion_error_payload=_chat_completion_error_payload,
            _downstream_stream_error_payload=_downstream_stream_error_payload,
            _get_header=_get_header,
            decoded_request_body=decoded_request_body,
            _sse_payload_bytes=_sse_payload_bytes,
            _chat_function_name_from_response_item=_chat_function_name_from_response_item,
    )


def _relay_context_for_handler(handler: Any) -> RelayContext:
    return RelayContext(
        handler=handler,
        symbols=_relay_symbols(),
        transparent_relay=handler._relay_transparent_upstream_response,
        official_passthrough_relay=handler._relay_official_passthrough_sse_response,
        prepared_exchange=getattr(handler, "_active_prepared_exchange", None),
    )


# Public facade aliases so tests patch and call ``codex_proxy.<name>`` rather than
# underscore-private internals. Private names remain for in-module callers.
official_urlopen = _official_urlopen
official_pool_manager = _official_pool_manager
official_proxy_url = _official_proxy_url
open_upstream_once = _open_upstream_once
open_upstream_response = _open_upstream_response
explicit_transport_phase = _explicit_transport_phase
diagnostic_error_connection_disposition = _diagnostic_error_connection_disposition
set_official_attempt_connection_disposition = _set_official_attempt_connection_disposition
connection_disposition = _connection_disposition
diagnostic_connection_disposition = _diagnostic_connection_disposition
observe_gateway_diagnostic = _observe_gateway_diagnostic
enqueue_gateway_event_payload = _enqueue_gateway_event_payload
activate_gateway_request = _activate_gateway_request
restore_gateway_request = _restore_gateway_request
active_gateway_request = _active_gateway_request
sleep_for_retry_with_gateway_cancellation = _sleep_for_retry_with_gateway_cancellation
published_catalog_model = _published_catalog_model
resolve_collaboration_boundary = _resolve_collaboration_boundary
collaboration_adapter = _collaboration_adapter
external_tool_protocol = _external_tool_protocol
codexhub_error_payload = _codexhub_error_payload
downstream_json_error_payload = _downstream_json_error_payload
downstream_sse_error_payload_for_inbound_format = _downstream_sse_error_payload_for_inbound_format
normalize_transparent_tool_schema_booleans = _normalize_transparent_tool_schema_booleans
strip_tools_for_compact_payload = _strip_tools_for_compact_payload
image_proxy_description_for_part = _image_proxy_description_for_part
image_proxy_cache_lookup = _image_proxy_cache_lookup
normalize_third_party_tool_call = _normalize_third_party_tool_call
downgrade_invalid_third_party_tool_calls = _downgrade_invalid_third_party_tool_calls
guard_duplicate_multi_agent_spawn_calls = _guard_duplicate_multi_agent_spawn_calls
adapt_third_party_apply_patch_response_body = _adapt_third_party_apply_patch_response_body
apply_patch_adapter = _apply_patch_adapter
rewrite_structured_tool_input_items = _rewrite_structured_tool_input_items
OfficialHTTPSConnection = _OfficialHTTPSConnection
OfficialHTTPSConnectionPool = _OfficialHTTPSConnectionPool
OfficialPooledResponse = _OfficialPooledResponse
TRANSPORT_PHASE_ATTRIBUTE = _TRANSPORT_PHASE_ATTRIBUTE


__all__ = [name for name in globals() if not name.startswith('__')]
