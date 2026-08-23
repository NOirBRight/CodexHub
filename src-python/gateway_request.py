"""Inbound request-boundary helpers for the CodexHub Gateway.

Owns request decoding and model extraction, Official reasoning sanitization,
browser-context detection, local Gateway authorization, request-context
headers, response-header filtering, reasoning-effort validation, and the
request-time Vision Proxy adapter factory.
"""

from __future__ import annotations

import gzip
import hmac
import io
import json
import zlib
from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any

import gateway_events
import proxy_telemetry
import vision_proxy
from catalog import canonical_model_id
import gateway_catalog_runtime as _catalog
import gateway_stream_semantics as _stream_semantics
from gateway_errors import safe_upstream_error_detail
import gateway_settings
from gateway_transport import _get_header, _header_items
from route_primitives import (
    IMAGE_PROXY_PROMPT,
    IMAGE_PROXY_PROMPT_VERSION,
    RouteProtocol,
)
from sse_events import SseEventAssembler
from vision_proxy import (
    IMAGE_PROXY_CACHE_LOCK,
    VisionFacts,
    VisionProxyAdapter,
    VisionProxyHooks,
)

try:
    import zstandard
except ImportError:  # pragma: no cover - optional dependency on older Python installs.
    zstandard = None

DECODE_ERRORS = (OSError, zlib.error) + ((zstandard.ZstdError,) if zstandard is not None else ())

OFFICIAL_ULTRA_REASONING_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra"}
OFFICIAL_ALIAS_PREFIX = "openai/"
UNSUPPORTED_REASONING_MODEL_PREFIXES = ("kimi-k2.6", "kimi-k2.7")
OFFICIAL_ENCRYPTED_CONTENT_PREFIX = "gAAAA"
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


def _has_browser_context_signal(value: Any) -> bool:
    for fragment in _stream_semantics._collect_text_fragments(value):
        lowered = fragment.lower()
        if any(marker in lowered for marker in BROWSER_CONTEXT_MARKERS):
            return True
        if BROWSER_CURRENT_URL_RE.search(fragment):
            return True
    return False


def _has_browser_context_guidance(value: Any) -> bool:
    return any(
        BROWSER_CONTEXT_GUIDANCE_SENTINEL in fragment
        for fragment in _stream_semantics._collect_text_fragments(value)
    )


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
        candidate = _catalog.generated_catalog_by_slug().get(
            _catalog.catalog_identity_slug(canonical_model_id(model))
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
    expected_key = gateway_settings.gateway_client_key()
    if expected_key is None:
        return True
    token = _bearer_token(headers)
    return bool(token and hmac.compare_digest(token, expected_key))


def _truthy_probe_value(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def raw_provider_probe_requested(headers: Mapping[str, str] | Any, path: str) -> bool:
    from urllib.parse import parse_qs, urlsplit

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
            gateway_events.RUNTIME_CODEX_DIR,
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


def _json_response_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


def provider_scoped_path(path: str, endpoint_suffix: str) -> str | None:
    from urllib.parse import unquote

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


def vision_proxy_adapter() -> VisionProxyAdapter:
    """Build a request-time Vision Proxy seam reading owning-module attributes."""

    import gateway_compat
    import gateway_transport
    import route_plan
    from protocol_translation import prepare_exchange

    return VisionProxyAdapter(
        facts=VisionFacts(
            cache_path=Path(vision_proxy.IMAGE_PROXY_CACHE_PATH),
            prompt_version=IMAGE_PROXY_PROMPT_VERSION,
            prompt=IMAGE_PROXY_PROMPT,
            cache_lock=IMAGE_PROXY_CACHE_LOCK,
            downstream_closed_error=_stream_semantics.DownstreamClosedDuringImageProxyError,
        ),
        hooks=VisionProxyHooks(
            enabled_reader=gateway_settings.gateway_image_proxy_enabled,
            vision_model_reader=gateway_settings.gateway_image_proxy_model,
            resolve_upstream=_catalog.choose_upstream,
            model_supports_image=_catalog.model_supports_image,
            canonical_model_id=canonical_model_id,
            compatible_request_body=gateway_compat.compatible_request_body,
            strip_tools=_stream_semantics._strip_tools_for_text_only_proxy_payload,
            responses_url=route_plan._responses_url,
            chat_completions_url=route_plan._chat_completions_url,
            prepare_exchange=prepare_exchange,
            upstream_headers=gateway_transport.upstream_headers,
            open_upstream=gateway_transport.open_upstream_response,
            upstream_timeout_seconds=gateway_settings.upstream_timeout_seconds,
            response_is_event_stream=_is_event_stream,
            events_to_responses_body=_stream_semantics._events_to_responses_body,
            write_event=gateway_events.write_adapter_event,
            usage_from_payload=gateway_events._usage_from_payload,
            normalize_usage=gateway_events.normalize_usage_for_event,
            safe_upstream_error_detail=safe_upstream_error_detail,
            cache_lookup_override=vision_proxy.image_proxy_cache_lookup,
            description_for_part_override=vision_proxy.image_proxy_description_for_part,
            sse_assembler_factory=SseEventAssembler,
            response_event_payload=_stream_semantics._converted_sse_payload,
        ),
    )


# Facade-era private alias kept for in-package callers.
_vision_proxy_adapter = vision_proxy_adapter


def _value_contains_image(value: Any) -> bool:
    return vision_proxy_adapter().value_contains_image(value)


def enforce_text_only_image_boundary(
    payload: dict[str, Any],
    *,
    inbound_format: str,
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    vision_plan: Any,
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Any = None,
) -> bool:
    from gateway_errors import ImageProxyError

    try:
        inbound_protocol = RouteProtocol(inbound_format)
    except ValueError as exc:
        raise ImageProxyError("Vision Proxy received an unsupported inbound protocol") from exc
    return vision_proxy_adapter().enforce_text_only_boundary(
        payload,
        inbound_protocol=inbound_protocol,
        target_model=target_model,
        target_upstream=target_upstream,
        vision_plan=vision_plan,
        event_context=event_context,
        progress_callback=progress_callback,
    )
