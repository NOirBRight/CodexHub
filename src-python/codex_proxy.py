from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
import hashlib
import io
import json
import logging
import os
import queue
import re
import sqlite3
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
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from catalog import (
    canonical_model_id,
    deny_match_model_id,
    load_catalog_models,
    load_policy,
    should_include_external_provider_model,
    should_include_model,
)
from catalog_sync import GENERATED_CATALOG_PATH, POLICY_PATH, existing_generated_catalog_path, sync_catalog
from codex_auth import CodexAuthError, access_token as codex_access_token, account_id as codex_account_id
from providers_config import resolve_external_model_alias, resolve_ollama_cloud_model
from subagent_state import build_subagent_state, state_guidance_message
import proxy_telemetry

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency on older Python installs.
    zstandard = None

DECODE_ERRORS = (OSError, zlib.error) + ((zstandard.ZstdError,) if zstandard is not None else ())

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
    "stream-read-error-retry-before-downstream",
    "downstream-sse-keepalive",
    "post-content-sse-idle-timeout",
    "browser-context-skill-guidance",
]
DEFAULT_OFFICIAL_PREFIXES = ("gpt-",)
OFFICIAL_ALIAS_PREFIX = "openai/"
OFFICIAL_FAST_VARIANT_SERVICE_TIER = "priority"
OFFICIAL_FAST_VARIANT_BASE_MODELS = {
    "gpt-5.5-fast": "gpt-5.5",
    "gpt-5.4-fast": "gpt-5.4",
}
OFFICIAL_FAST_VARIANT_DISPLAY_NAMES = {
    "gpt-5.5-fast": "OpenAI GPT-5.5 Fast",
    "gpt-5.4-fast": "OpenAI GPT-5.4 Fast",
}
OLLAMA_REASONING_EFFORT_ALIASES = {"xhigh": "max"}
UNSUPPORTED_REASONING_MODEL_PREFIXES = ("kimi-k2.6",)
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
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300
DEFAULT_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS = 90.0
DEFAULT_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS = 60.0
DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS = 3
DEFAULT_GATEWAY_AUTO_RETRY_MAX_ATTEMPTS = 30
RETRY_REQUEST_MAIN_GENERATION = "main_generation"
RETRY_REQUEST_COMPACT = "compact"
RETRY_REQUEST_IMAGE_PROXY_VISION = "image_proxy_vision"
RETRY_REQUEST_OFFICIAL_CONTROL = "official_control"
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
    "authentication_error",
    "billing_hard_limit_reached",
    "billing_not_active",
    "context_length_exceeded",
    "insufficient_quota",
    "invalid_api_key",
    "invalid_image",
    "invalid_request_error",
    "model_not_found",
    "not_found_error",
    "permission_denied",
    "permission_error",
    "unsupported_image",
    "unsupported_parameter",
    "unsupported_value",
}
IMAGE_PROXY_PROMPT_VERSION = "v2"
IMAGE_PROXY_PROMPT = (
    "Describe the image in detail for a downstream text-only model. "
    "Include visible text, objects, layout, colors, charts, and any details "
    "that may affect the user's request. Return only the final visual "
    "description. Do not include reasoning, caveats, or mention that you are a proxy."
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


class UpstreamStreamIncompleteError(RuntimeError):
    """Raised when an upstream stream ends without a terminal event."""


class UpstreamStreamIdleTimeoutError(TimeoutError):
    """Raised when an upstream SSE stream stalls before completion."""

    def __init__(self, timeout_seconds: float, phase: str = "post_output"):
        self.timeout_seconds = timeout_seconds
        self.phase = phase
        detail = "before output started" if phase == "pre_output" else "after output started"
        super().__init__(f"Upstream stream produced no real event for {timeout_seconds:g} seconds {detail}.")


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


def gateway_retry_delay_seconds(attempt: int) -> int:
    return min(max(1, attempt) * 2, 8)


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


def write_proxy_event(event: str, **fields: Any) -> None:
    payload = proxy_telemetry.prepare_event_payload(event, fields, RUNTIME_CODEX_DIR)
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    try:
        with PROXY_EVENT_LOG_LOCK:
            PROXY_EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with PROXY_EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
    except OSError as exc:
        logger.warning("failed to write proxy event log: %s", type(exc).__name__)


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
        }

    official_alias = official_alias_upstream_model(slug, policy)
    if official_alias is not None:
        return {
            "name": "official",
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "upstream_model": official_alias,
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
        }

    raise ValueError(f"model is not in the generated cloud catalog: {slug}")


def official_upstream() -> dict[str, Any]:
    return {
        "name": "official",
        "base_url": official_base_url(),
        "auth": "codex_auth",
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


def _responses_sse_line_resets_idle_timeout(line: bytes) -> bool:
    event = _parse_sse_json_payload(line)
    if not isinstance(event, Mapping):
        return False
    return _responses_event_starts_downstream_output(event) or _responses_events_have_terminal([event])


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
    if not isinstance(payload, Mapping):
        return False
    return _chat_stream_chunk_starts_downstream_output(payload) or _chat_stream_chunk_has_finish(payload)


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
        if item_type == "message":
            role = item.get("role")
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


def _chat_completion_tool_outputs(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
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


def _chat_completion_to_response_body(body: bytes) -> bytes:
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

    _hide_reasoning_text(response_payload)
    response_payload, _ = _normalize_third_party_tool_call(response_payload)
    response_payload, _ = _downgrade_invalid_third_party_tool_calls(response_payload)
    return json.dumps(response_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


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
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            tool_calls = delta.get("tool_calls")
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
                        state["name"] = name
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
        text = "".join(text_parts)
        if text:
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
        events.append(
            {
                "type": "response.completed",
                "response": {
                    "id": f"resp_{uuid.uuid4().hex[:12]}",
                    "object": "response",
                    "status": "completed",
                    "output": output,
                },
            }
        )

    return events


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


def _response_body_to_chat_completion_body(body: bytes) -> bytes:
    """Convert a Responses API response body into a Chat Completions response body."""
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return body
    if "error" in payload and not isinstance(payload.get("output"), list):
        return _chat_completion_error_body(payload)

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    output = payload.get("output")
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
                name = item.get("name")
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
                state["name"] = item.get("name") or state["name"]
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

    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if event_type == "response.created":
            resp = event.get("response")
            if isinstance(resp, Mapping):
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

    payload: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": output,
    }
    if usage is not None:
        payload["usage"] = dict(usage)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


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
        return None

    return _developer_text_message("[Compacted conversation context]\n" + "\n\n".join(fragments))


def _developer_text_message(content: str) -> dict[str, str]:
    return {"type": "message", "role": "developer", "content": content}


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
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


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
        target_agent_ids = open_agent_ids
        if name == "wait_agent" and wait_agent_ids is not None:
            target_agent_ids = wait_agent_ids
        elif name == "close_agent" and close_agent_ids is not None:
            target_agent_ids = close_agent_ids
        if target_agent_ids and name in {"wait_agent", "close_agent"}:
            ids_text = ", ".join(target_agent_ids)
            description += f" Current open agent_id target(s): {ids_text}. Use these id(s) next."
            properties = parameters.setdefault("properties", {})
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
    if namespace == "multi_agent_v1" and isinstance(name, str) and name in MULTI_AGENT_TOOL_NAMES:
        return name
    if isinstance(name, str):
        alias = THIRD_PARTY_TOOL_NAME_ALIASES.get(name)
        if alias:
            return alias
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
    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue
        if item.get("type") == "function_call":
            rewritten = _structured_tool_function_call_item(item)
            rewritten_items.append(rewritten if rewritten is not None else item)
            changed = changed or rewritten != item
            continue
        if item.get("type") == "function_call_output":
            rewritten_items.append(dict(item))
            continue
        replacement = _compatible_internal_message(item)
        if replacement is not None:
            rewritten_items.append(replacement)
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

    for tool in additions:
        name = _tool_schema_name(tool)
        if not name or name in existing_names:
            continue
        tools.append(tool)
        existing_names.add(name)
        changed = True
    return changed


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


def _json_string_value(value: str) -> Any:
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _coerce_targets(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        parsed = _json_string_value(value)
        if isinstance(parsed, list):
            return parsed, True
        if isinstance(parsed, str):
            return [parsed], True
        return [value], True
    return value, False


def _coerce_target(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        parsed = _json_string_value(value)
        if isinstance(parsed, list) and parsed:
            return parsed[0], True
        if isinstance(parsed, str) and parsed != value:
            return parsed, True
        return value, False
    if isinstance(value, list) and value:
        return value[0], True
    return value, False


def _coerce_number(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text), True
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)", text):
            return float(text), True
    return value, False


def _infer_multi_agent_tool_name(arguments: Mapping[str, Any]) -> str | None:
    if "targets" in arguments:
        return "wait_agent"
    if "target" in arguments:
        return "send_input" if "message" in arguments else "close_agent"
    if "id" in arguments:
        return "resume_agent"
    if any(key in arguments for key in ("agent_type", "fork_context", "message")):
        return "spawn_agent"
    return None


def _codex_apps_flat_alias_name(name: Any) -> str | None:
    if not isinstance(name, str) or not name.startswith("mcp__codex_apps__"):
        return None
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
        return name
    return None


def _split_namespace_tool_alias(name: Any) -> tuple[str, str] | None:
    if not isinstance(name, str):
        return None
    if _codex_apps_flat_alias_name(name) is not None:
        return None
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
    arguments = _json_object_from_arguments(value)
    if arguments is None:
        return None

    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return None

    normalized: dict[str, Any] = {"query": query}
    limit = arguments.get("limit")
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit.strip())
    if isinstance(limit, int) and limit > 0:
        normalized["limit"] = limit
    return normalized


def _is_multi_agent_discovery_arguments(arguments: Mapping[str, Any] | None) -> bool:
    if not arguments:
        return False
    query = arguments.get("query")
    if not isinstance(query, str):
        return False
    lowered = query.lower()
    return all(term in lowered for term in ("spawn_agent", "multi_agent", "subagent"))


def _multi_agent_discovery_arguments(value: Any) -> dict[str, Any] | None:
    arguments = _json_object_from_arguments(value)
    if arguments is None:
        return None

    if arguments:
        return None

    return {"query": MULTI_AGENT_DISCOVERY_QUERY, "limit": 8}


def _normalize_multi_agent_arguments(
    value: Any,
    tool_name: str | None,
) -> tuple[Any, str | None, bool]:
    arguments = _json_object_from_arguments(value)
    if arguments is None:
        return value, tool_name, False

    changed = False
    resolved_tool_name = tool_name
    if resolved_tool_name is None:
        for key in ("", "tool", "function", "name", "action", "ns_tool", "operation", "method", "tool_name"):
            candidate = arguments.get(key)
            if isinstance(candidate, str) and candidate in MULTI_AGENT_TOOL_NAMES:
                resolved_tool_name = candidate
                arguments.pop(key, None)
                changed = True
                break
    if resolved_tool_name is None:
        resolved_tool_name = _infer_multi_agent_tool_name(arguments)

    for key in ("fork_context", "interrupt"):
        item = arguments.get(key)
        if isinstance(item, str) and item.lower() in {"true", "false"}:
            arguments[key] = item.lower() == "true"
            changed = True

    if "targets" in arguments:
        coerced, item_changed = _coerce_targets(arguments["targets"])
        if item_changed:
            arguments["targets"] = coerced
            changed = True
    if "target" in arguments:
        coerced, item_changed = _coerce_target(arguments["target"])
        if item_changed:
            arguments["target"] = coerced
            changed = True
    if "timeout_ms" in arguments:
        coerced, item_changed = _coerce_number(arguments["timeout_ms"])
        if item_changed:
            arguments["timeout_ms"] = coerced
            changed = True

    if not changed:
        return value, resolved_tool_name, False
    return _dump_arguments_like(value, arguments), resolved_tool_name, True


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
        flat_namespace_alias = _codex_apps_namespace_flat_alias(value.get("namespace"), value.get("name"))
        if flat_namespace_alias is not None:
            rewritten["name"] = flat_namespace_alias
            rewritten.pop("namespace", None)
            changed = True
        else:
            original_name = value.get("name")
            tool_name = THIRD_PARTY_TOOL_NAME_ALIASES.get(original_name) if isinstance(original_name, str) else None
            namespace_alias = None
            if tool_name is None and isinstance(original_name, str) and original_name in MULTI_AGENT_TOOL_NAMES:
                tool_name = original_name
            if tool_name is None:
                namespace_alias = _split_namespace_tool_alias(original_name)
            argument_key = "arguments" if "arguments" in value else "input" if "input" in value else None
            if original_name in MULTI_AGENT_NAMESPACE_ALIASES and argument_key is not None:
                normalized, tool_name, args_changed = _normalize_multi_agent_arguments(value.get(argument_key), None)
                if args_changed:
                    rewritten[argument_key] = normalized
                    changed = True
            elif tool_name is not None and argument_key is not None:
                normalized, _, args_changed = _normalize_multi_agent_arguments(value.get(argument_key), tool_name)
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
        "required_next_action: write the final concise report now. The lifecycle already completed via real Codex tool calls; do not infer hidden tools were unavailable, and do not call tool_search or any multi_agent_v1 tool again for this single-loop request."
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
            "required_next_action: call multi_agent_v1__close_agent with target set to one of these agent_id values before writing the final report."
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


def _guard_duplicate_multi_agent_spawn_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    tool_protocol = str((event_context or {}).get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools"}:
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

    return _guard_duplicate_multi_agent_spawn_calls_inner(
        value,
        spawn_allowed=spawn_allowed,
        subagent_state=subagent_state,
        lifecycle_complete=lifecycle_complete,
        wait_agent_ids=wait_agent_ids,
        open_agent_ids=open_agent_ids,
    )


def _guard_duplicate_multi_agent_spawn_calls_inner(
    value: Any,
    *,
    spawn_allowed: bool,
    subagent_state: Any | None,
    lifecycle_complete: bool,
    wait_agent_ids: list[str],
    open_agent_ids: list[str],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _guard_duplicate_multi_agent_spawn_calls_inner(
                item,
                spawn_allowed=spawn_allowed,
                subagent_state=subagent_state,
                lifecycle_complete=lifecycle_complete,
                wait_agent_ids=wait_agent_ids,
                open_agent_ids=open_agent_ids,
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
                    return value, False
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
                        "required_next_action: write the final concise report now. "
                        "The requested subagent lifecycle is already complete."
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
            spawn_allowed=spawn_allowed,
            subagent_state=subagent_state,
            lifecycle_complete=lifecycle_complete,
            wait_agent_ids=wait_agent_ids,
            open_agent_ids=open_agent_ids,
        )
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


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


def compatible_request_body(
    body: bytes,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
    event_context: Mapping[str, Any] | None = None,
    inject_codex_tools: bool = True,
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
    if upstream_name == "official":
        changed = _sanitize_official_reasoning_items(payload)
        if _normalize_responses_string_input(payload):
            changed = True
        if _sanitize_official_system_messages(payload):
            changed = True
        if _sanitize_official_invalid_tool_calls(payload):
            changed = True
        if _inject_browser_context_guidance(payload, upstream_name=upstream_name, event_context=event_context):
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

    tool_protocol = _external_tool_protocol(upstream)
    if isinstance(event_context, dict):
        event_context["tool_protocol"] = tool_protocol
    if tool_protocol in STRUCTURED_TOOL_PROTOCOLS:
        changed = _rewrite_structured_tool_input_items(payload, event_context=event_context, upstream_name=upstream_name)
    elif tool_protocol == "none":
        tools = payload.get("tools")
        if isinstance(tools, list):
            filtered_tools = [tool for tool in tools if not _is_multi_agent_tool_schema(tool)]
            if len(filtered_tools) != len(tools):
                payload["tools"] = filtered_tools
                changed = True
        else:
            changed = False
    else:
        changed = _rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name)
    input_items = payload.get("input")
    if _inject_browser_context_guidance(payload, upstream_name=upstream_name, event_context=event_context):
        changed = True
        input_items = payload.get("input")
    include_tool_search = False
    subagent_state = build_subagent_state(input_items) if tool_protocol in {"text_compat", "chat_tools"} else None
    subagent_state_active = subagent_state is not None and (
        bool(subagent_state.agents) or subagent_state.requested_count is not None
    )
    node_repl_single_step_complete = _has_completed_single_step_node_repl_context(input_items)

    if subagent_state_active and subagent_state is not None:
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
        state_hint = state_guidance_message(subagent_state) if tool_protocol == "text_compat" else None
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
    if isinstance(event_context, dict):
        if subagent_state is not None:
            event_context["_subagent_state"] = subagent_state
        event_context["subagent_open_agent_ids"] = list(open_agent_ids)
        event_context["subagent_wait_agent_ids"] = list(wait_agent_ids)
        event_context["subagent_close_agent_ids"] = list(close_agent_ids)
        event_context["subagent_spawn_allowed"] = bool(include_spawn_agent)
        event_context["subagent_lifecycle_complete"] = bool(lifecycle_complete)
    if state_hint is not None and isinstance(input_items, list):
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
    if node_repl_single_step_complete and isinstance(input_items, list):
        input_items.append(_node_repl_single_step_complete_message())
        _write_adapter_event(
            event_context,
            "node_repl_single_step_complete_guidance_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        )
        changed = True
    allow_codex_tools = tool_protocol != "none"
    if inject_codex_tools and allow_codex_tools:
        tool_names_before = _function_tool_names(payload.get("tools"))
        if _inject_explicit_codex_tools(
            payload,
            include_tool_search=include_tool_search,
            include_multi_agent_tools=not lifecycle_complete,
            include_spawn_agent=include_spawn_agent,
            include_wait_agent=include_wait_agent,
            include_close_agent=include_close_agent,
            include_resume_agent=include_resume_agent,
            include_send_input=include_send_input,
            include_node_repl_tools=not node_repl_single_step_complete,
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

    if "reasoning" in payload and _reasoning_param_is_unsupported(upstream_name, requested_model, upstream_model):
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
    if upstream_name == "official":
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
    payload, invalid_tool_changed = _downgrade_invalid_third_party_tool_calls(payload)
    changed = changed or invalid_tool_changed
    payload, duplicate_spawn_changed = _guard_duplicate_multi_agent_spawn_calls(payload, event_context)
    changed = changed or duplicate_spawn_changed
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def compatible_sse_line(
    line: bytes,
    upstream_name: str,
    event_context: Mapping[str, Any] | None = None,
) -> bytes:
    if upstream_name == "official" or not line.startswith(b"data:"):
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
    payload, invalid_tool_changed = _downgrade_invalid_third_party_tool_calls(payload)
    changed = changed or invalid_tool_changed
    payload, duplicate_spawn_changed = _guard_duplicate_multi_agent_spawn_calls(payload, event_context)
    changed = changed or duplicate_spawn_changed
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


def _header_items(headers: Mapping[str, str] | Any) -> list[tuple[str, str]]:
    return [(str(key), str(value)) for key, value in headers.items()]


def _get_header(headers: Mapping[str, str] | Any, name: str) -> str | None:
    wanted = name.lower()
    for key, value in _header_items(headers):
        if key.lower() == wanted:
            return value
    return None


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
    if "codex" in value:
        return "codex-app"
    return None


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
) -> dict[str, str]:
    auth_mode = upstream.get("auth")
    outgoing: dict[str, str] = {}

    for key, value in _header_items(incoming_headers):
        lowered = key.lower()
        if lowered in HOP_BY_HOP_REQUEST_HEADERS or lowered == "authorization":
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
        token = codex_access_token()
        outgoing["Authorization"] = f"Bearer {token}"
        # The chatgpt.com backend requires the account id header to identify
        # the subscription. Inject it from auth.json when not already present.
        if not _get_header(outgoing, "Chatgpt-account-id"):
            account = codex_account_id()
            if account:
                outgoing["Chatgpt-account-id"] = account
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
    return catalog_with_official_fast_variants(
        json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    )


def catalog_with_official_fast_variants(catalog: dict[str, Any]) -> dict[str, Any]:
    models = catalog.get("models")
    if not isinstance(models, list):
        return catalog

    by_slug = {
        canonical_model_id(str(model.get("slug", ""))): model
        for model in models
        if isinstance(model, Mapping)
    }
    for fast_model, upstream_model in OFFICIAL_FAST_VARIANT_BASE_MODELS.items():
        base_slug = f"{OFFICIAL_ALIAS_PREFIX}{upstream_model}"
        fast_slug = f"{OFFICIAL_ALIAS_PREFIX}{fast_model}"
        base_model = by_slug.get(base_slug)
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


def _json_response_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


KNOWN_UPSTREAM_ENDPOINT_SUFFIXES = ("/chat/completions", "/responses", "/messages", "/models")


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
    return urlsplit(base_url).path.rstrip("/").lower().endswith(path.lower())


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


def _image_description_part(description: str) -> dict[str, str]:
    return {
        "type": "input_text",
        "text": (
            "The Gateway has already read the user's attached image. "
            "Use the visual context below as the image content when answering. "
            "Do not mention the Gateway, preprocessing, replacement, missing images, "
            "or inability to view the original attachment. Answer directly.\n\n"
            f"Visual context:\n{description}"
        ),
    }


def _replace_image_parts(value: Any, describe: Any) -> tuple[Any, bool]:
    if _is_image_part(value):
        return _image_description_part(describe(value)), True
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

    vision_model = gateway_image_proxy_model()
    if not vision_model:
        raise ImageProxyError("Vision model is not configured for Image Proxy")
    try:
        vision_upstream = choose_upstream(vision_model)
    except ValueError as exc:
        raise ImageProxyError(f"Vision model is not available: {vision_model}: {exc}") from exc
    if not model_supports_image(vision_model, vision_upstream):
        raise ImageProxyError(f"Vision model does not support image input: {vision_model}")

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

    def describe(part: Mapping[str, Any]) -> str:
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
        return descriptions[cache_key]

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
        return 1
    if request_kind == RETRY_REQUEST_OFFICIAL_CONTROL:
        return 1
    return 3


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
        return b""
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


def _http_error_values(exc: HTTPError) -> set[str]:
    payload = _http_error_payload(exc)
    if not isinstance(payload, Mapping):
        return set()
    error = payload.get("error")
    values: set[str] = set()
    if isinstance(error, Mapping):
        for key in ("type", "code", "param", "message"):
            value = error.get(key)
            if isinstance(value, str) and value:
                values.add(value.strip().lower())
    elif isinstance(error, str) and error:
        values.add(error.strip().lower())
    for key in ("type", "code", "detail", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            values.add(value.strip().lower())
    return values


def _has_permanent_upstream_error_value(exc: HTTPError) -> bool:
    values = _http_error_values(exc)
    if not values:
        return False
    for value in values:
        if value in PERMANENT_UPSTREAM_ERROR_VALUES:
            return True
    return False


def _upstream_error_retryable(
    exc: BaseException,
    *,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
) -> bool:
    if isinstance(exc, HTTPError):
        override = _http_retry_header_override(exc)
        if override is not None:
            return override
        status = _upstream_retry_status(exc)
        if status in PERMANENT_HTTP_ERROR_STATUSES:
            return False
        if status == 429 and _has_permanent_upstream_error_value(exc):
            return False
        if _has_permanent_upstream_error_value(exc):
            return False
        if status in TRANSIENT_HTTP_RETRY_STATUSES:
            return True
        if status is not None and 520 <= status <= 599:
            return True
        return False
    return isinstance(exc, (IncompleteRead, OSError, URLError))


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
) -> None:
    _write_adapter_event(
        event_context,
        "upstream_retry",
        upstream=upstream_name,
        provider_id=upstream_name,
        upstream_format=upstream_format,
        request_kind=request_kind,
        retryable=True,
        status=_upstream_retry_status(exc),
        attempt=attempt,
        max_attempts=max_attempts,
        delay_ms=delay_seconds * 1000,
        error=type(exc).__name__,
        detail=safe_upstream_error_detail(exc),
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
) -> dict[str, Any]:
    return {
        "type": "codexhub.retry",
        "upstream": upstream_name,
        "upstream_format": upstream_format,
        "request_kind": request_kind,
        "status": _upstream_retry_status(exc),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "delay_ms": delay_seconds * 1000,
        "error": type(exc).__name__,
        "detail": safe_upstream_error_detail(exc),
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
    return {
        "type": "upstream_stream_error",
        "status": status,
        "upstream": upstream_name,
        "error": error_type,
        "detail": error_detail,
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
        }
    }


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
) -> Any:
    retry_attempts = _upstream_retry_attempts(request_kind) if max_attempts is None else max(1, max_attempts)
    for attempt in range(1, retry_attempts + 1):
        try:
            return urlopen(request, timeout=timeout)
        except (HTTPError, IncompleteRead, OSError, URLError) as exc:
            if attempt >= retry_attempts or not _upstream_error_retryable(exc, request_kind=request_kind):
                raise
            delay_seconds = gateway_retry_delay_seconds(attempt)
            _emit_upstream_retry_event(
                event_context,
                upstream_name=upstream_name,
                upstream_format=upstream_format,
                request_kind=request_kind,
                attempt=attempt,
                max_attempts=retry_attempts,
                exc=exc,
                delay_seconds=delay_seconds,
            )
            if downstream_retry_callback is not None:
                downstream_retry_callback(
                    _downstream_retry_payload(
                        upstream_name=upstream_name,
                        upstream_format=upstream_format,
                        request_kind=request_kind,
                        attempt=attempt,
                        max_attempts=retry_attempts,
                        exc=exc,
                        delay_seconds=delay_seconds,
                    )
                )
            time.sleep(delay_seconds)
    raise RuntimeError("unreachable upstream retry state")


class CodexProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
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
        request_kind = RETRY_REQUEST_MAIN_GENERATION
        proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
        model = None
        model_requested = None
        upstream_name = None
        upstream_format = "responses"
        downstream_sse_started = False

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            content_type = _get_header(self.headers, "Content-Type")
            content_encoding = _get_header(self.headers, "Content-Encoding")
            body, content_decoded, decode_error = decoded_request_body(body, content_encoding)
            if decode_error:
                raise ValueError(f"request body content-encoding decode failed: {decode_error}")
            try:
                inbound_payload = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                inbound_payload = None
            request_kind = _request_kind_from_headers_and_payload(self.headers, inbound_payload, inbound_format)
            if request_kind == RETRY_REQUEST_COMPACT:
                proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
                if isinstance(inbound_payload, dict) and _strip_tools_for_compact_payload(
                    inbound_payload,
                    event_context={"request_id": request_id, **proxy_request_context},
                ):
                    body = json.dumps(inbound_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            # Convert inbound Chat Completions request to Responses format before routing.
            if inbound_format == "chat_completions":
                body = _chat_completions_request_to_responses_body(body)
            # Capture the caller's desired stream mode before compatible_request_body
            # forces stream=true for the official upstream.
            try:
                caller_stream = json.loads(body.decode("utf-8-sig")).get("stream") is True
            except (UnicodeDecodeError, json.JSONDecodeError):
                caller_stream = True
            model_requested = try_extract_model(body)
            model = provider_scoped_route_model(model_requested, provider_hint)
            if provider_hint is not None and not model:
                raise ValueError(f"model is required for provider path: {provider_hint}")
            route_reason = "provider_path" if provider_hint and model else "model" if model else "official_control_fallback"
            upstream = choose_upstream(model) if model else official_upstream()
            upstream_name = upstream["name"]
            upstream_format = str(upstream.get("upstream_format", "responses"))
            model_canonical = canonical_model_id(model) if model else None
            request_observability = proxy_telemetry.enrich_request_observability(
                body=body,
                codex_home=RUNTIME_CODEX_DIR,
                upstream=upstream,
            )
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
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                inbound_format=inbound_format,
                is_stream=caller_stream,
                content_length=content_length,
                decoded_content_length=len(body) if content_decoded else None,
                content_type=content_type[:120] if content_type else None,
                content_encoding=content_encoding[:80] if content_encoding else None,
                content_decoded=content_decoded,
                decode_error=decode_error[:160] if decode_error else None,
                **request_observability,
                **proxy_request_context,
            )
            adapter_event_context = {
                "request_id": request_id,
                "model": model_canonical,
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
            body = compatible_request_body(
                body,
                upstream,
                model_id=model,
                event_context=adapter_event_context,
                inject_codex_tools=request_kind != RETRY_REQUEST_COMPACT,
            )
            try:
                image_proxy_payload = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                image_proxy_payload = None
            if isinstance(image_proxy_payload, dict) and apply_image_proxy_to_responses_payload(
                image_proxy_payload,
                model,
                upstream,
                event_context=adapter_event_context,
                progress_callback=emit_downstream_status if caller_stream else None,
            ):
                body = json.dumps(image_proxy_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            responses_body = body
            headers = upstream_headers(self.headers, upstream, drop_content_encoding=content_decoded)
            emit_retry_to_downstream = caller_stream and inbound_format == "responses"

            def upstream_request_for_format(selected_format: str) -> Request:
                if selected_format == "chat_completions":
                    return Request(
                        _chat_completions_url(upstream),
                        data=_responses_request_to_chat_completion_body(responses_body),
                        headers=headers,
                        method="POST",
                    )
                return Request(
                    _responses_url(upstream, "/v1/responses"),
                    data=responses_body,
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
                request = upstream_request_for_format(selected_upstream_format)
                relay_attempts = _upstream_retry_attempts(request_kind)
                try:
                    for relay_attempt in range(1, relay_attempts + 1):
                        try:
                            with _open_upstream_response(
                                request,
                                upstream_name=upstream_name,
                                upstream_format=selected_upstream_format,
                                timeout=upstream_timeout_seconds(),
                                event_context=adapter_event_context,
                                downstream_retry_callback=emit_downstream_retry if emit_retry_to_downstream else None,
                                request_kind=request_kind,
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
                                    defer_stream_errors=relay_attempt < relay_attempts and not downstream_sse_started,
                                    mark_downstream_sse_started=mark_downstream_sse_started,
                                )
                            break
                        except (
                            CompactEmptyResponseError,
                            IncompleteRead,
                            UpstreamStreamInterruptedError,
                            UpstreamStreamIncompleteError,
                        ) as exc:
                            retry_exc = exc.cause if isinstance(exc, UpstreamStreamInterruptedError) else exc
                            if relay_attempt >= relay_attempts:
                                raise retry_exc
                            delay_seconds = gateway_retry_delay_seconds(relay_attempt)
                            _emit_upstream_retry_event(
                                adapter_event_context,
                                upstream_name=upstream_name,
                                upstream_format=selected_upstream_format,
                                request_kind=request_kind,
                                attempt=relay_attempt,
                                max_attempts=relay_attempts,
                                exc=retry_exc,
                                delay_seconds=delay_seconds,
                            )
                            emit_downstream_retry(
                                _downstream_retry_payload(
                                    upstream_name=upstream_name,
                                    upstream_format=selected_upstream_format,
                                    request_kind=request_kind,
                                    attempt=relay_attempt,
                                    max_attempts=relay_attempts,
                                    exc=retry_exc,
                                    delay_seconds=delay_seconds,
                                )
                            )
                            time.sleep(delay_seconds)
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
        except ImageProxyError as exc:
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
                error=type(exc).__name__,
                detail=str(exc)[:300],
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
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
                upstream_name=upstream_name or "upstream_error",
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
                    **proxy_request_context,
                }
                status = self._relay_upstream_response(
                    exc,
                    "upstream_error",
                    request_id=request_id,
                    model=canonical_model_id(model) if model else None,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    event_context=adapter_event_context,
                )
            except OSError as relay_exc:
                self.close_connection = True
                write_proxy_event(
                    "client_write_failed",
                    request_id=request_id,
                    model=canonical_model_id(model) if model else None,
                    upstream=upstream_name,
                    upstream_format=upstream_format,
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
                status=status,
                error="HTTPError",
                detail=safe_upstream_error_detail(exc),
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
                status=502,
                error=type(exc).__name__,
                detail=detail,
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
                status=502,
                error=type(exc).__name__,
                detail=detail,
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
        pre_output_timeout_seconds = pre_output_sse_idle_timeout_seconds()
        post_output_timeout_seconds = post_content_sse_idle_timeout_seconds()
        pre_output_idle_guard_enabled = pre_output_timeout_seconds > 0 and line_resets_idle_timeout is not None
        post_output_idle_guard_enabled = (
            post_output_timeout_seconds > 0
            and (downstream_output_started is not None or line_resets_idle_timeout is not None)
        )
        if keepalive_interval <= 0 and not pre_output_idle_guard_enabled and not post_output_idle_guard_enabled:
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
        idle_reset_seen = False
        post_output_idle_guard_active = False
        last_progress_at = stream_started_at
        last_keepalive_at = stream_started_at

        def mark_idle_reset_seen() -> None:
            nonlocal idle_reset_seen, post_output_idle_guard_active, last_progress_at
            idle_reset_seen = True
            post_output_idle_guard_active = post_output_idle_guard_enabled
            last_progress_at = time.monotonic()

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
            if (
                post_output_idle_guard_enabled
                and not idle_reset_seen
                and downstream_output_started is not None
                and downstream_output_started()
            ):
                mark_idle_reset_seen()
                now = time.monotonic()

            timeout_seconds: float | None = None
            if keepalive_interval > 0:
                timeout_seconds = max(0.001, keepalive_interval - (now - last_keepalive_at))
            if pre_output_idle_guard_enabled and not idle_reset_seen:
                remaining_idle = pre_output_timeout_seconds - (now - stream_started_at)
                if remaining_idle <= 0:
                    raise_idle_timeout(pre_output_timeout_seconds, "pre_output")
                timeout_seconds = (
                    remaining_idle
                    if timeout_seconds is None
                    else max(0.001, min(timeout_seconds, remaining_idle))
                )
            if post_output_idle_guard_active:
                remaining_idle = post_output_timeout_seconds - (now - last_progress_at)
                if remaining_idle <= 0:
                    raise_idle_timeout(post_output_timeout_seconds, "post_output")
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
                if pre_output_idle_guard_enabled and not idle_reset_seen:
                    if (now - stream_started_at) >= pre_output_timeout_seconds:
                        raise_idle_timeout(pre_output_timeout_seconds, "pre_output")
                if post_output_idle_guard_active and (now - last_progress_at) >= post_output_timeout_seconds:
                    raise_idle_timeout(post_output_timeout_seconds, "post_output")
                if keepalive_interval > 0:
                    self._write_sse_keepalive()
                    last_keepalive_at = time.monotonic()
                continue
            if kind == "error":
                raise value
            if isinstance(value, bytes) and value:
                if line_resets_idle_timeout is not None and line_resets_idle_timeout(value):
                    mark_idle_reset_seen()
                elif post_output_idle_guard_active and line_resets_idle_timeout is None:
                    last_progress_at = time.monotonic()
                elif post_output_idle_guard_active and (time.monotonic() - last_progress_at) >= post_output_timeout_seconds:
                    raise_idle_timeout(post_output_timeout_seconds, "post_output")
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
        if inbound_format == "chat_completions":
            self.wfile.write(
                b"data: "
                + json.dumps(
                    _chat_completion_error_payload(
                        upstream_name=upstream_name,
                        status=status,
                        exc=exc,
                        error=error,
                        detail=detail,
                        error_type="upstream_stream_error",
                    ),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n\n"
            )
            self.wfile.flush()
            self.close_connection = True
            return
        if exc is not None:
            self._write_sse_error_event(upstream_name, exc)
            self.close_connection = True
            return
        self._write_sse_protocol_error_event(
            upstream_name,
            status,
            detail or error or "upstream stream failed",
            error=error or "UpstreamProtocolError",
        )
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
        self._safe_send_json(
            status,
            _json_error_payload_for_inbound_format(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=status,
                exc=exc,
                error=error,
                detail=detail,
                error_type=error_type,
            ),
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
                _capture_usage(usage_capture, _usage_from_json_body(body))
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
            if want_chat_output:
                if upstream_format == "chat_completions":
                    # Upstream already returned Chat Completions; pass through.
                    pass
                else:
                    # Upstream returned Responses format; convert to Chat Completions.
                    body = _response_body_to_chat_completion_body(body)
            elif upstream_format == "chat_completions":
                body = compatible_response_body(
                    _chat_completion_to_response_body(body),
                    upstream_name,
                    event_context=event_context,
                )
            else:
                body = compatible_response_body(body, upstream_name, event_context=event_context)
            _capture_usage(usage_capture, _usage_from_json_body(body))
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
            if headers_already_sent:
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

        if not headers_already_sent:
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
            if mark_downstream_sse_started is not None:
                mark_downstream_sse_started()

        if is_event_stream:
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
                            _capture_usage(usage_capture, _usage_from_response_event(event))
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
                    chunks = _response_events_to_chat_stream_chunks(events, require_completed=True)
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

                for chunk in chunks:
                    self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
                _capture_usage(usage_capture, None)
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
                            _capture_usage(usage_capture, _usage_from_payload(payload))
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
                if want_chat_output:
                    # Inbound and upstream are both Chat Completions; pass through.
                    for chunk in chunks:
                        if chunk == "[DONE]":
                            continue
                        self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
                        self.wfile.flush()
                else:
                    for event in _chat_stream_chunks_to_response_events(chunks):
                        event, _ = _normalize_third_party_tool_call(event)
                        event, _ = _downgrade_invalid_third_party_tool_calls(event)
                        event, _ = _guard_duplicate_multi_agent_spawn_calls(event, event_context)
                        self.wfile.write(_sse_json_line(event, line_ending) + line_ending)
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
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
            saw_response_event = False
            saw_terminal_event = False
            downstream_output_started = False
            pending_sse_event_metadata: list[bytes] = []
            drop_next_sse_separator = False
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
                        self.wfile.write(line)
                        self.wfile.flush()
                        continue
                    if upstream_name != "official" and _is_sse_event_metadata_line(line):
                        pending_sse_event_metadata.append(line)
                        continue
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
                    line = compatible_sse_line(line, upstream_name, event_context=event_context)
                    rewritten_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                    _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payload)

                    if not line and upstream_name != "official":
                        pending_sse_event_metadata = []
                        drop_next_sse_separator = True
                        continue

                    if pending_sse_event_metadata:
                        for metadata_line in pending_sse_event_metadata:
                            self.wfile.write(metadata_line)
                        pending_sse_event_metadata = []
                    self.wfile.write(line)
                    if saw_terminal_event:
                        separator = _sse_event_separator_after_line(line)
                        if separator:
                            self.wfile.write(separator)
                    self.wfile.flush()
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
                if defer_stream_errors and not saw_response_event:
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
                self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream Responses stream ended without a terminal event.",
                )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
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
            _capture_usage(usage_capture, None)
            return status

        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True
        _capture_usage(usage_capture, None)
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
