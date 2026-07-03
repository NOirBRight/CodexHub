from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import re
import sqlite3
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import tomllib
from typing import Any, Mapping
import uuid
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from catalog import canonical_model_id, load_catalog_models, load_policy, should_include_model
from catalog_sync import GENERATED_CATALOG_PATH, POLICY_PATH, existing_generated_catalog_path, sync_catalog
from codex_auth import CodexAuthError, access_token as codex_access_token, account_id as codex_account_id
from providers_config import resolve_external_model_alias
import proxy_telemetry

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency on older Python installs.
    zstandard = None

DECODE_ERRORS = (OSError, zlib.error) + ((zstandard.ZstdError,) if zstandard is not None else ())

OFFICIAL_BASE_URL = "https://api.openai.com/v1"
OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
PROXY_BUILD = "2026-06-30-subagent-single-loop-completion-gate"
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
    "third-party-tool-search-hidden-after-discovery",
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
]
DEFAULT_OFFICIAL_PREFIXES = ("gpt-",)
OFFICIAL_ALIAS_PREFIX = "openai/"
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
PROXY_TELEMETRY_DB_PATH = proxy_telemetry.telemetry_db_path(RUNTIME_CODEX_DIR)
PROXY_TEXT_LOG_PATH = RUNTIME_PROXY_DIR / "codex-proxy.log"
PROXY_EVENT_LOG_LOCK = threading.Lock()
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 300
DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS = 3

logger = logging.getLogger("codex_proxy")


def upstream_timeout_seconds() -> int:
    raw_value = os.environ.get("CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS")
    if not raw_value:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_UPSTREAM_TIMEOUT_SECONDS


def official_upstream_open_attempts() -> int:
    raw_value = os.environ.get("CODEX_PROXY_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS")
    if not raw_value:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS
    return value if value > 0 else DEFAULT_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS


def write_proxy_event(event: str, **fields: Any) -> None:
    payload = proxy_telemetry.prepare_event_payload(event, fields, RUNTIME_CODEX_DIR)
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    try:
        with PROXY_EVENT_LOG_LOCK:
            PROXY_EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with PROXY_EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                try:
                    proxy_telemetry.write_event_to_sqlite(PROXY_TELEMETRY_DB_PATH, payload)
                except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
                    failure_payload = proxy_telemetry.prepare_event_payload(
                        "telemetry_sqlite_write_failed",
                        {
                            "request_id": fields.get("request_id"),
                            "error": type(exc).__name__,
                            "detail": str(exc)[:240],
                        },
                        RUNTIME_CODEX_DIR,
                    )
                    handle.write(json.dumps(failure_payload, ensure_ascii=True, separators=(",", ":")) + "\n")
                    logger.warning("failed to write proxy telemetry sqlite: %s", type(exc).__name__)
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
    payload = dict(event_context)
    payload.update(fields)
    write_proxy_event(event, **payload)


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


def official_alias_upstream_model(slug: str, policy: Any) -> str | None:
    if not slug.startswith(OFFICIAL_ALIAS_PREFIX):
        return None
    upstream_model = slug[len(OFFICIAL_ALIAS_PREFIX) :]
    if upstream_model.startswith(official_prefixes()) and should_include_model(upstream_model, policy):
        return upstream_model
    return None


def choose_upstream(model_id: str) -> dict[str, Any]:
    slug = canonical_model_id(str(model_id))
    if not slug:
        raise ValueError("model is required")

    policy = load_policy(POLICY_PATH)
    official_alias = official_alias_upstream_model(slug, policy)
    if official_alias is not None:
        return {
            "name": "official",
            "base_url": official_base_url(),
            "auth": "codex_auth",
            "upstream_model": official_alias,
        }

    if not should_include_model(slug, policy):
        raise ValueError(f"model is not allowed: {slug}")

    if slug.startswith(official_prefixes()):
        return {
            "name": "official",
            "base_url": official_base_url(),
            "auth": "codex_auth",
        }

    external_model = resolve_external_model_alias(slug)
    if external_model is not None:
        return {
            "name": external_model["upstream_name"],
            "base_url": external_model["base_url"],
            "auth": "api_key",
            "api_key": external_model["api_key"],
            "upstream_model": external_model["upstream_model"],
            "upstream_format": external_model.get("upstream_format", "responses"),
        }

    if "/" in slug:
        raise ValueError(f"external provider model is not configured: {slug}")

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


def _sse_payload_bytes(line: bytes) -> bytes | None:
    if not line.startswith(b"data:"):
        return None

    content = line
    for candidate in (b"\r\n", b"\n", b"\r"):
        if line.endswith(candidate):
            content = line[: -len(candidate)]
            break

    payload_bytes = content[5:].lstrip()
    if not payload_bytes or payload_bytes == b"[DONE]":
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


def _sse_json_line(payload: Mapping[str, Any], line_ending: bytes) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + line_ending


def _chat_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    fragments = _collect_text_fragments(value)
    return "\n".join(fragments)


def _responses_input_to_chat_messages(value: Any) -> list[dict[str, str]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list):
        return []

    messages: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = item.get("role")
        role = role if role in {"system", "user", "assistant"} else "user"
        content = _chat_content_text(item.get("content"))
        messages.append({"role": role, "content": content})
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


def _chat_stream_chunks_to_response_events(chunks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
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
        if output:
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


def _response_events_to_chat_stream_chunks(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert Responses API SSE events into Chat Completions stream chunks.

    Mirrors :func:`_chat_stream_chunks_to_response_events`.  Text deltas become
    ``delta.content`` fragments; function_call argument deltas become
    ``delta.tool_calls`` fragments.  A final chunk with ``finish_reason`` is
    emitted when the response completes.
    """
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


def _events_to_responses_body(events: list[Mapping[str, Any]]) -> bytes:
    """Reconstruct a non-streaming Responses API body from SSE events.

    Used when the upstream forces streaming (e.g. chatgpt.com) but the caller
    requested a non-streaming response.  Collects output items and text from
    the event stream into a single ``response`` object.
    """
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

    return {
        "type": "message",
        "role": "system",
        "content": "[Compacted conversation context]\n" + "\n\n".join(fragments),
    }


def _system_text_message(content: str) -> dict[str, str]:
    return {"type": "message", "role": "system", "content": content}


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


def _system_transcript_message(title: str, item: Mapping[str, Any]) -> dict[str, str]:
    return {"type": "message", "role": "system", "content": _transcript_text(title, item)}


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


def _inject_explicit_codex_tools(
    payload: dict[str, Any],
    include_tool_search: bool = True,
    include_multi_agent_tools: bool = True,
    include_spawn_agent: bool = True,
    include_wait_agent: bool = True,
    include_close_agent: bool = True,
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
                open_agent_ids=open_agent_ids,
                wait_agent_ids=wait_agent_ids,
                close_agent_ids=close_agent_ids,
            )
        )
    additions.extend(_flatten_namespace_function_tools(tools))

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


def _split_namespace_tool_alias(name: Any) -> tuple[str, str] | None:
    if not isinstance(name, str):
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
    return _system_text_message("\n".join(lines))


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


def _open_multi_agent_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    open_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        text = _joined_text(item.get("content"))
        if "Codex native multi_agent_v1.spawn_agent result" in text and "status: succeeded" in text:
            agent_id = _line_value(text, "agent_id:")
            if agent_id:
                open_agent_ids.add(agent_id)
        if "Codex native multi_agent_v1.close_agent result" in text and "status: closed" in text:
            closed_agent_id = _line_value(text, "closed_agent_id:")
            if closed_agent_id:
                open_agent_ids.discard(closed_agent_id)
            else:
                open_agent_ids.clear()
    return sorted(open_agent_ids)


def _split_agent_id_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def _completed_multi_agent_wait_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    completed_agent_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        text = _joined_text(item.get("content"))
        if "Codex native multi_agent_v1.wait_agent result" not in text or "status: completed" not in text:
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
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        text = _joined_text(item.get("content"))
        if "Codex native multi_agent_v1.close_agent result" not in text or "status: closed" not in text:
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
            "不要再 spawn",
            "不要重复验证",
            "不要重复",
            "only once",
            "single spawn",
            "single loop",
            "do not spawn again",
            "don't spawn again",
            "do not repeat",
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
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        text = _joined_text(item.get("content"))
        if "Codex native multi_agent_v1.spawn_agent result" in text and "status: succeeded" in text:
            if not _line_value(text, "agent_id:"):
                unknown_open_agent = True
        if "Codex native multi_agent_v1.close_agent result" in text and "status: closed" in text:
            if not _line_value(text, "closed_agent_id:"):
                unknown_open_agent = False
    return unknown_open_agent


def _multi_agent_lifecycle_complete_message(closed_agent_ids: list[str]) -> dict[str, str]:
    lines = ["Codex native multi_agent_v1 current state"]
    lines.append("status: lifecycle_complete")
    if closed_agent_ids:
        lines.append(f"closed_agent_ids: {', '.join(closed_agent_ids)}")
    lines.append(
        "required_next_action: write the final concise report now. Do not call tool_search or any multi_agent_v1 tool again for this single-loop request."
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
    return _system_text_message("\n".join(lines))


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
        for label, key in (("function", "name"), ("call_id", "call_id"), ("status", "status")):
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
    return {"type": "message", "role": "system", "content": "\n".join(lines)}


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
    multi_agent_search_call_ids: set[str] = set()
    multi_agent_calls_by_call_id: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for item in input_items:
        item_type = item.get("type") if isinstance(item, dict) else None
        if isinstance(item_type, str) and item_type in INTERNAL_INPUT_ITEM_TYPES:
            call_id = item.get("call_id")
            if item_type == "function_call" and isinstance(call_id, str):
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
        if _sanitize_official_invalid_tool_calls(payload):
            changed = True
        if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
            payload["model"] = upstream_model
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
        if not changed:
            return body
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    changed = _rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name)
    input_items = payload.get("input")
    include_tool_search = not _has_multi_agent_discovery_context(input_items)
    open_agent_ids = _open_multi_agent_ids(input_items)
    completed_wait_agent_ids = set(_completed_multi_agent_wait_ids(input_items))
    closed_agent_ids = _closed_multi_agent_ids(input_items)
    wait_agent_ids = [agent_id for agent_id in open_agent_ids if agent_id not in completed_wait_agent_ids]
    close_agent_ids = [agent_id for agent_id in open_agent_ids if agent_id in completed_wait_agent_ids]
    has_open_agent = _has_open_multi_agent_context(input_items)
    lifecycle_complete = _has_completed_single_loop_multi_agent_context(input_items)
    if lifecycle_complete:
        include_tool_search = False
    include_spawn_agent = not has_open_agent
    include_wait_agent = (not has_open_agent) or not open_agent_ids or bool(wait_agent_ids)
    include_close_agent = (not has_open_agent) or not open_agent_ids or bool(close_agent_ids)
    if lifecycle_complete:
        include_spawn_agent = False
        include_wait_agent = False
        include_close_agent = False
        state_hint = _multi_agent_lifecycle_complete_message(closed_agent_ids)
    else:
        state_hint = _multi_agent_current_state_message(wait_agent_ids, close_agent_ids)
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
    tool_names_before = _function_tool_names(payload.get("tools"))
    if _inject_explicit_codex_tools(
        payload,
        include_tool_search=include_tool_search,
        include_multi_agent_tools=not lifecycle_complete,
        include_spawn_agent=include_spawn_agent,
        include_wait_agent=include_wait_agent,
        include_close_agent=include_close_agent,
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
) -> list[tuple[str, str]]:
    outgoing: list[tuple[str, str]] = []
    for key, value in _header_items(headers):
        lowered = key.lower()
        if lowered in HOP_BY_HOP_RESPONSE_HEADERS:
            continue
        if lowered == "content-length" and (is_event_stream or content_length is not None):
            continue
        outgoing.append((key, value))
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
    return json.loads(catalog_path.read_text(encoding="utf-8-sig"))


def _json_response_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


def _responses_url(upstream: Mapping[str, Any], request_path: str) -> str:
    parsed = urlsplit(request_path)
    path = parsed.path
    if path.startswith("/v1/"):
        path = path[3:]
    elif not path.startswith("/"):
        path = "/" + path
    url = upstream["base_url"].rstrip("/") + path
    if parsed.query:
        url += "?" + parsed.query
    return url


def _chat_completions_url(upstream: Mapping[str, Any]) -> str:
    return upstream["base_url"].rstrip("/") + "/chat/completions"


def _open_upstream_response(
    request: Request,
    *,
    upstream_name: str,
    upstream_format: str,
    timeout: int,
    event_context: Mapping[str, Any] | None = None,
) -> Any:
    max_attempts = official_upstream_open_attempts() if upstream_name == "official" else 1
    for attempt in range(1, max_attempts + 1):
        try:
            return urlopen(request, timeout=timeout)
        except HTTPError:
            raise
        except (OSError, URLError) as exc:
            if attempt >= max_attempts:
                raise
            _write_adapter_event(
                event_context,
                "upstream_open_retry",
                upstream=upstream_name,
                upstream_format=upstream_format,
                attempt=attempt,
                max_attempts=max_attempts,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
            )
            time.sleep(min(0.25 * attempt, 1.0))
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

        if parsed.path == "/v1/chat/completions":
            self._proxy_post_request(inbound_format="chat_completions")
            return

        self._send_json(404, {"error": "not found"})

    def _proxy_post_request(self, *, inbound_format: str) -> None:
        """Shared POST handler for inbound Responses and Chat Completions requests.

        ``inbound_format`` is the wire format the *caller* used.  When it is
        ``chat_completions`` the request body is converted to Responses format
        before routing, and the upstream response is converted back to Chat
        Completions format before being returned to the caller.
        """
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        model = None
        upstream_name = None
        upstream_format = "responses"

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            content_type = _get_header(self.headers, "Content-Type")
            content_encoding = _get_header(self.headers, "Content-Encoding")
            body, content_decoded, decode_error = decoded_request_body(body, content_encoding)
            if decode_error:
                raise ValueError(f"request body content-encoding decode failed: {decode_error}")
            # Convert inbound Chat Completions request to Responses format before routing.
            if inbound_format == "chat_completions":
                body = _chat_completions_request_to_responses_body(body)
            # Capture the caller's desired stream mode before compatible_request_body
            # forces stream=true for the official upstream.
            try:
                caller_stream = json.loads(body.decode("utf-8-sig")).get("stream") is True
            except (UnicodeDecodeError, json.JSONDecodeError):
                caller_stream = True
            model = try_extract_model(body)
            route_reason = "model" if model else "official_control_fallback"
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
                model_requested=model,
                model_canonical=model_canonical,
                upstream=upstream_name,
                provider_id=upstream_name,
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
                **request_context,
            )
            adapter_event_context = {
                "request_id": request_id,
                "model": model_canonical,
                **request_context,
            }
            usage_capture: dict[str, Any] = {}
            body = compatible_request_body(body, upstream, model_id=model, event_context=adapter_event_context)
            upstream_url = _responses_url(upstream, "/v1/responses")
            if upstream_format == "chat_completions":
                body = _responses_request_to_chat_completion_body(body)
                upstream_url = _chat_completions_url(upstream)
            headers = upstream_headers(self.headers, upstream, drop_content_encoding=content_decoded)
            request = Request(upstream_url, data=body, headers=headers, method="POST")
            with _open_upstream_response(
                request,
                upstream_name=upstream_name,
                upstream_format=upstream_format,
                timeout=upstream_timeout_seconds(),
                event_context=adapter_event_context,
            ) as response:
                status = self._relay_upstream_response(
                    response,
                    upstream_name,
                    request_id=request_id,
                    model=model_canonical,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    caller_stream=caller_stream,
                    event_context=adapter_event_context,
                    usage_capture=usage_capture,
                )
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=model_canonical,
                model_requested=model,
                model_canonical=model_canonical,
                upstream=upstream_name,
                provider_id=upstream_name,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                is_stream=caller_stream,
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_observability,
                **usage_capture,
                **request_context,
            )
        except ValueError as exc:
            write_proxy_event(
                "request_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                status=400,
                error=type(exc).__name__,
                detail=str(exc)[:300],
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._safe_send_json(400, {"error": str(exc)}, request_id)
        except HTTPError as exc:
            try:
                adapter_event_context = {
                    "request_id": request_id,
                    "model": canonical_model_id(model) if model else None,
                    **request_context,
                }
                status = self._relay_upstream_response(
                    exc,
                    "upstream_error",
                    request_id=request_id,
                    model=canonical_model_id(model) if model else None,
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
                    **request_context,
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
                **request_context,
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
                **request_context,
            )
            self._safe_send_json(502, {"error": type(exc).__name__, "detail": detail}, request_id)
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
                **request_context,
            )
            self._safe_send_json(502, {"error": type(exc).__name__, "detail": detail}, request_id)
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
                **request_context,
            )
            self._safe_send_json(500, {"error": type(exc).__name__, "detail": detail}, request_id)

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
                route_reason="official_control",
                content_length=0,
                **request_context,
            )
            request = Request(_responses_url(upstream, self.path), headers=headers, method=method)
            with urlopen(request, timeout=upstream_timeout_seconds()) as response:
                status = self._relay_upstream_response(response, upstream_name, request_id=request_id, model=None)
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason="official_control",
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
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
                    route_reason="official_control",
                    status=getattr(exc, "code", 502),
                    error=type(relay_exc).__name__,
                    detail=safe_upstream_error_detail(relay_exc),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **request_context,
                )
                return
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason="official_control",
                status=status,
                error="HTTPError",
                detail=safe_upstream_error_detail(exc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
        except (OSError, URLError) as exc:
            detail = safe_upstream_error_detail(exc)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason="official_control",
                status=502,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._safe_send_json(502, {"error": type(exc).__name__, "detail": detail}, request_id)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_response_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if not is_event_stream or buffer_sse_to_json:
            if buffer_sse_to_json:
                # Buffer the full SSE stream into a list of events.
                events: list[Mapping[str, Any]] = []
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
                # Reconstruct a Responses-format body from the events.
                body = _events_to_responses_body(events)
                _capture_usage(usage_capture, _usage_from_json_body(body))
                is_event_stream = False
            else:
                body = b""
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    body += chunk
            if want_chat_output:
                if upstream_format == "chat_completions":
                    # Upstream already returned Chat Completions; pass through.
                    pass
                else:
                    # Upstream returned Responses format; convert to Chat Completions.
                    body = _response_body_to_chat_completion_body(body)
            elif upstream_format == "chat_completions":
                body = _chat_completion_to_response_body(body)
            else:
                body = compatible_response_body(body, upstream_name, event_context=event_context)
            _capture_usage(usage_capture, _usage_from_json_body(body))

        self.send_response(status)
        content_length = None if is_event_stream else len(body)
        for key, value in _filtered_response_headers(response.headers, is_event_stream, content_length):
            self.send_header(key, value)
        self.send_header("X-Codex-Proxy-Upstream", upstream_name)
        self.send_header("Connection", "close")
        self.end_headers()

        if is_event_stream:
            if want_chat_output and upstream_format != "chat_completions":
                # Upstream returns Responses SSE; convert to Chat Completions SSE.
                line_ending = b"\n"
                events: list[Mapping[str, Any]] = []
                while True:
                    line = response.readline()
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
                for chunk in _response_events_to_chat_stream_chunks(events):
                    self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
                _capture_usage(usage_capture, None)
                return status

            if upstream_format == "chat_completions":
                line_ending = b"\n"
                chunks: list[Mapping[str, Any]] = []
                while True:
                    line = response.readline()
                    if not line:
                        break
                    line_ending = _sse_line_ending(line)
                    payload_bytes = _sse_payload_bytes(line)
                    if payload_bytes is None:
                        continue
                    try:
                        payload = json.loads(payload_bytes.decode("utf-8-sig"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(payload, dict):
                        chunks.append(payload)
                        _capture_usage(usage_capture, _usage_from_payload(payload))
                if want_chat_output:
                    # Inbound and upstream are both Chat Completions; pass through.
                    for chunk in chunks:
                        self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n")
                        self.wfile.flush()
                else:
                    for event in _chat_stream_chunks_to_response_events(chunks):
                        event, _ = _normalize_third_party_tool_call(event)
                        event, _ = _downgrade_invalid_third_party_tool_calls(event)
                        self.wfile.write(_sse_json_line(event, line_ending))
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
            try:
                while True:
                    line = response.readline()
                    if not line:
                        break
                    original_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                    usage_payload = _parse_sse_json_payload(line)
                    if isinstance(usage_payload, Mapping):
                        _capture_usage(usage_capture, _usage_from_response_event(usage_payload))
                    line = compatible_sse_line(line, upstream_name, event_context=event_context)
                    rewritten_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                    _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payload)

                    self.wfile.write(line)
                    self.wfile.flush()
            except IncompleteRead as exc:
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
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
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
