"""Typed Vision Proxy request adapter for the Python Gateway.

This module owns the Vision Proxy seam: image detection and fail-closed boundary
checks, persistent description caching, image-model requests, response text
extraction, and Responses/Chat Completions payload replacement.  Catalog and
runtime resolution, protocol codecs, transport, telemetry, and downstream
orchestration are read from their owning modules at call time, without a
callback bag or dependency registry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Protocol
from urllib.request import Request

from gateway_interfaces import UpstreamResponseLike
from gateway_errors import ImageProxyError, UpstreamStreamIncompleteError
from route_primitives import (
    IMAGE_PROXY_PROMPT,
    IMAGE_PROXY_PROMPT_VERSION,
    RETRY_REQUEST_IMAGE_PROXY_VISION,
    VISION_PROXY_CODEX_APP_ADAPTER,
    VISION_PROXY_DISABLED,
    VISION_PROXY_TRANSPARENT_OVERLAY,
    RouteProtocol,
    VisionAction,
    VisionNetworkAction,
)


from gateway_settings import runtime_proxy_dir as _runtime_proxy_dir

logger = logging.getLogger("vision_proxy")
IMAGE_PROXY_CACHE_LOCK = threading.Lock()
IMAGE_PROXY_CACHE_PATH = _runtime_proxy_dir() / "image-proxy-cache.sqlite"

# Request-time injectables. Tests patch these; unset (None) means use the
# Gateway runtime default implementation.
image_proxy_cache_lookup = None
image_proxy_description_for_part = None


class VisionProxyPolicy(Enum):
    """Typed Route Plan policy understood by the Vision Proxy seam."""

    DISABLED = VISION_PROXY_DISABLED
    CODEX_APP_ADAPTER = VISION_PROXY_CODEX_APP_ADAPTER
    TRANSPARENT_OVERLAY = VISION_PROXY_TRANSPARENT_OVERLAY


class VisionPlanLike(Protocol):
    policy: str
    action: VisionAction
    network_action: VisionNetworkAction
    target_accepts_images: bool
    image_proxy_enabled: bool


class RequestCompatibilityHook(Protocol):
    def __call__(
        self,
        body: bytes,
        upstream: Mapping[str, Any],
        *,
        model_id: str | None = None,
        event_context: Mapping[str, Any] | None = None,
        inject_codex_tools: bool = True,
    ) -> bytes: ...


class ResponseEventPayloadHook(Protocol):
    def __call__(self, event: SseFrameLike) -> Mapping[str, Any] | str | None: ...


class BoundaryOverrideHook(Protocol):
    def __call__(
        self,
        payload: dict[str, Any],
        *,
        inbound_format: str,
        target_model: str | None,
        target_upstream: Mapping[str, Any],
        vision_proxy_policy: str,
        image_proxy_enabled: bool | None,
        target_accepts_images: bool | None,
        event_context: Mapping[str, Any] | None,
        progress_callback: Callable[[Mapping[str, Any]], bool] | None,
    ) -> bool: ...


class ToolStripHook(Protocol):
    def __call__(
        self,
        payload: dict[str, Any],
        *,
        event_context: Mapping[str, Any] | None = None,
        upstream_name: str | None = None,
        event_name: str,
    ) -> bool: ...


class PreparedExchangeLike(Protocol):
    upstream_body: bytes


class SseFrameLike(Protocol):
    data: bytes
    lines: Iterable[Any]


class SseTerminationLike(Protocol):
    disposition: str
    events: Iterable[SseFrameLike]


class SseAssemblerLike(Protocol):
    def feed(self, chunk: bytes) -> Iterable[SseFrameLike]: ...
    def finish(self) -> SseTerminationLike: ...


class PrepareExchangeHook(Protocol):
    def __call__(
        self,
        body: bytes,
        *,
        inbound_format: str,
        outbound_format: str,
    ) -> PreparedExchangeLike: ...


class OpenUpstreamHook(Protocol):
    def __call__(
        self,
        request: Request,
        *,
        upstream_name: str,
        upstream_format: str,
        timeout: int,
        event_context: Mapping[str, Any] | None,
        request_kind: str,
        max_attempts: int,
    ) -> AbstractContextManager[UpstreamResponseLike]: ...


def _false() -> bool:
    return False


def _empty_text() -> str:
    return ""


def _identity_model(value: str) -> str:
    return value.strip()


def _unbound_upstream(_model: str) -> Mapping[str, Any]:
    raise RuntimeError("Vision Proxy catalog hook is not bound")


def _unsupported_model(_model: str, _upstream: Mapping[str, Any]) -> bool:
    return False


def _unbound_request_compatibility(
    _body: bytes,
    _upstream: Mapping[str, Any],
    **_kwargs: Any,
) -> bytes:
    raise RuntimeError("Vision Proxy request compatibility hook is not bound")


def _no_tools(
    _payload: dict[str, Any],
    **_kwargs: Any,
) -> bool:
    return False


def _unbound_url(_upstream: Mapping[str, Any], *_args: Any) -> str:
    raise RuntimeError("Vision Proxy URL hook is not bound")


def _unbound_headers(
    _headers: Mapping[str, str],
    _upstream: Mapping[str, Any],
) -> dict[str, str]:
    raise RuntimeError("Vision Proxy transport header hook is not bound")


def _unbound_open(_request: Request, **_kwargs: Any) -> Any:
    raise RuntimeError("Vision Proxy transport hook is not bound")


def _default_timeout() -> int:
    return 300


def _not_event_stream(_headers: Any) -> bool:
    return False


def _unbound_events_to_body(_events: list[Mapping[str, Any]]) -> bytes:
    raise RuntimeError("Vision Proxy Responses event codec hook is not bound")


def _unbound_prepare_exchange(_body: bytes, **_kwargs: Any) -> Any:
    raise RuntimeError("Vision Proxy protocol translation hook is not bound")


def _noop_event(
    _event_context: Mapping[str, Any] | None,
    _event: str,
    **_fields: Any,
) -> None:
    return None


def _empty_usage(_payload: Any) -> Mapping[str, Any] | None:
    return None


def _missing_usage(_usage: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return {"usage_source": "missing", "usage_missing_reason": "upstream_missing_usage"}


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


@dataclass(frozen=True)
class VisionFacts:
    """Immutable constants and persistence facts for one Vision Proxy adapter."""

    cache_path: Path = Path("image-proxy-cache.sqlite")
    prompt_version: str = IMAGE_PROXY_PROMPT_VERSION
    prompt: str = IMAGE_PROXY_PROMPT
    cache_lock: Any = field(default_factory=lambda: IMAGE_PROXY_CACHE_LOCK)
    downstream_closed_error: type[BaseException] = RuntimeError


def is_image_part(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    part_type = value.get("type")
    if part_type == "input_image":
        return any(
            (
                isinstance(value.get(key), str) and bool(value.get(key))
                for key in ("image_url", "file_id")
            )
        )
    if part_type == "image_url":
        image_url = value.get("image_url")
        return (
            isinstance(image_url, Mapping)
            and isinstance(image_url.get("url"), str)
            and bool(image_url.get("url"))
        )
    return False


def value_contains_image(value: Any) -> bool:
    if is_image_part(value):
        return True
    if isinstance(value, list):
        return any((value_contains_image(item) for item in value))
    if isinstance(value, Mapping):
        return any((value_contains_image(item) for item in value.values()))
    return False


def normalized_image_part(part: Mapping[str, Any]) -> dict[str, Any]:
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


def cache_key(part: Mapping[str, Any], vision_model: str) -> str:
    raw = json.dumps(
        {
            "image": normalized_image_part(part),
            "vision_model": vision_model,
            "prompt_version": _facts().prompt_version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def unique_image_count(value: Any, vision_model: str) -> int:
    cache_keys: set[str] = set()

    def collect(item: Any) -> None:
        if is_image_part(item):
            cache_keys.add(cache_key(item, vision_model))
        elif isinstance(item, list):
            for child in item:
                collect(child)
        elif isinstance(item, Mapping):
            for child in item.values():
                collect(child)

    collect(value)
    return len(cache_keys)


def _ensure_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        "\n            CREATE TABLE IF NOT EXISTS image_proxy_cache (\n                cache_key TEXT PRIMARY KEY,\n                vision_model TEXT NOT NULL,\n                prompt_version TEXT NOT NULL,\n                description TEXT NOT NULL,\n                created_at INTEGER NOT NULL\n            )\n            "
    )


def cache_lookup(cache_key: str) -> str | None:
    override = image_proxy_cache_lookup or _cache_lookup_override
    if override is not None:
        return override(cache_key)
    path = Path(_facts().cache_path)
    try:
        with _facts().cache_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                _ensure_cache(conn)
                row = conn.execute(
                    "SELECT description FROM image_proxy_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        logger.warning("Vision Proxy cache lookup failed: %s", type(exc).__name__)
        return None
    if not row:
        return None
    description = row[0]
    return description if isinstance(description, str) and description else None


def cache_store(cache_key: str, vision_model: str, description: str) -> None:
    if _cache_store_override is not None:
        _cache_store_override(cache_key, vision_model, description)
        return
    path = Path(_facts().cache_path)
    try:
        with _facts().cache_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                _ensure_cache(conn)
                conn.execute(
                    "\n                        INSERT OR REPLACE INTO image_proxy_cache\n                        (cache_key, vision_model, prompt_version, description, created_at)\n                        VALUES (?, ?, ?, ?, ?)\n                        ",
                    (
                        cache_key,
                        vision_model,
                        _facts().prompt_version,
                        description,
                        int(_wall_time()),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        logger.warning("Vision Proxy cache store failed: %s", type(exc).__name__)


def extract_response_text(payload: Any) -> str:
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
                    if (
                        isinstance(part, Mapping)
                        and part.get("type") in {"output_text", "text"}
                        and isinstance(part.get("text"), str)
                        and part.get("text")
                    ):
                        text_parts.append(part["text"])
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
                        if (
                            isinstance(part, Mapping)
                            and part.get("type") == "text"
                            and isinstance(part.get("text"), str)
                            and part.get("text")
                        ):
                            text_parts.append(part["text"])
    return "\n".join((part.strip() for part in text_parts if part.strip())).strip()


def _event_payload(event: SseFrameLike) -> Mapping[str, Any] | str | None:
    if not any((line.name == b"data" for line in event.lines)) or not event.data:
        return None
    if event.data == b"[DONE]":
        return None
    try:
        payload = json.loads(event.data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageProxyError("Vision model returned a malformed SSE response") from exc
    if not isinstance(payload, Mapping):
        raise ImageProxyError("Vision model returned a malformed SSE response")
    return payload


def response_body(response: Any) -> bytes:
    if _response_body_override is not None:
        return _response_body_override(response)
    if _response_is_event_stream(response.headers):
        events: list[Mapping[str, Any]] = []
        if _sse_assembler_factory is None:
            raise ImageProxyError("Vision Proxy SSE parser is not configured")
        assembler = _sse_assembler_factory()
        event_payload = _response_event_payload or _event_payload
        while True:
            chunk = response.readline()
            if not chunk:
                break
            for frame in assembler.feed(chunk):
                payload = event_payload(frame)
                if payload == "[DONE]" or payload is None:
                    continue
                if not isinstance(payload, Mapping):
                    raise ImageProxyError(
                        "Vision model returned an unsupported SSE payload"
                    )
                events.append(payload)
        termination = assembler.finish()
        if termination.disposition == "incomplete":
            raise UpstreamStreamIncompleteError(
                "Vision Proxy SSE stream ended with an incomplete pending frame"
            )
        for frame in termination.events:
            payload = event_payload(frame)
            if payload == "[DONE]" or payload is None:
                continue
            if not isinstance(payload, Mapping):
                raise ImageProxyError(
                    "Vision model returned an unsupported SSE payload"
                )
            events.append(payload)
        return _events_to_responses_body(events)
    chunks: list[bytes] = []
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def call_vision_model_for_description(
    part: Mapping[str, Any],
    vision_model: str,
    vision_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> str:
    if _describe_image_override is not None:
        return _describe_image_override(
            part, vision_model, vision_upstream, event_context
        )
    started_at = _monotonic()
    try:
        upstream_format = RouteProtocol(
            str(vision_upstream.get("upstream_format") or RouteProtocol.RESPONSES.value)
        )
    except ValueError as exc:
        raise ImageProxyError("Vision model uses an unsupported protocol") from exc
    if upstream_format not in {RouteProtocol.RESPONSES, RouteProtocol.CHAT_COMPLETIONS}:
        raise ImageProxyError("Vision model uses an unsupported protocol")
    payload = {
        "model": vision_model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _facts().prompt},
                    normalized_image_part(part),
                ],
            }
        ],
        "stream": upstream_format is RouteProtocol.RESPONSES,
    }
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    vision_context = dict(event_context or {})
    vision_context["image_proxy"] = True
    vision_context["vision_model"] = _canonical_model_id(vision_model)
    try:
        body = _compatible_request_body(
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
        upstream_name = str(vision_upstream.get("name", "unknown"))
        if isinstance(vision_payload, dict) and _strip_tools(
            vision_payload,
            event_context=vision_context,
            upstream_name=upstream_name,
            event_name="image_proxy_vision_tools_stripped",
        ):
            body = json.dumps(
                vision_payload, ensure_ascii=True, separators=(",", ":")
            ).encode("utf-8")
        if upstream_format is RouteProtocol.CHAT_COMPLETIONS:
            exchange = _prepare_exchange(
                body,
                inbound_format=RouteProtocol.RESPONSES.value,
                outbound_format=RouteProtocol.CHAT_COMPLETIONS.value,
            )
            body = exchange.upstream_body
            upstream_url = _chat_completions_url(vision_upstream)
        else:
            upstream_url = _responses_url(vision_upstream, "/v1/responses")
        headers = _upstream_headers(
            {"Content-Type": "application/json"}, vision_upstream
        )
    except ValueError as exc:
        raise ImageProxyError(f"Vision model request is invalid: {exc}") from exc
    request = Request(upstream_url, data=body, headers=headers, method="POST")
    vision_upstream_name = str(vision_upstream.get("name", "unknown"))
    _write_event(
        event_context,
        "image_proxy_vision_request_start",
        vision_model=_canonical_model_id(vision_model),
        upstream=vision_upstream_name,
        upstream_format=upstream_format.value,
        stream=payload["stream"],
    )
    try:
        with _open_upstream(
            request,
            upstream_name=vision_upstream_name,
            upstream_format=upstream_format.value,
            timeout=_upstream_timeout_seconds(),
            event_context=vision_context,
            request_kind=RETRY_REQUEST_IMAGE_PROXY_VISION,
            max_attempts=1,
        ) as response:
            response_status = getattr(response, "status", None)
            response_bytes = response_body(response)
    except BaseException as exc:
        _write_event(
            event_context,
            "image_proxy_vision_request_error",
            vision_model=_canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format.value,
            duration_ms=int((_monotonic() - started_at) * 1000),
            error=type(exc).__name__,
            detail=_safe_upstream_error_detail(exc),
        )
        raise
    try:
        response_payload = json.loads(response_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _write_event(
            event_context,
            "image_proxy_vision_request_error",
            vision_model=_canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format.value,
            duration_ms=int((_monotonic() - started_at) * 1000),
            status=response_status if isinstance(response_status, int) else None,
            error=type(exc).__name__,
            detail="Vision model returned an invalid response",
        )
        raise ImageProxyError("Vision model returned an invalid response") from exc
    description = (
        _response_text_override(response_payload)
        if _response_text_override is not None
        else extract_response_text(response_payload)
    )
    if not description:
        _write_event(
            event_context,
            "image_proxy_vision_request_error",
            vision_model=_canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format.value,
            duration_ms=int((_monotonic() - started_at) * 1000),
            status=response_status if isinstance(response_status, int) else None,
            error="EmptyImageDescription",
            detail="Vision model returned no image description",
            **dict(_normalize_usage(_usage_from_payload(response_payload))),
        )
        raise ImageProxyError("Vision model returned no image description")
    _write_event(
        event_context,
        "image_proxy_vision_request_complete",
        vision_model=_canonical_model_id(vision_model),
        upstream=vision_upstream_name,
        upstream_format=upstream_format.value,
        duration_ms=int((_monotonic() - started_at) * 1000),
        status=response_status if isinstance(response_status, int) else None,
        description_length=len(description),
        **dict(_normalize_usage(_usage_from_payload(response_payload))),
    )
    return description


def description_for_part(
    part: Mapping[str, Any],
    vision_model: str,
    vision_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> str:
    override = image_proxy_description_for_part or _description_for_part_override
    if override is not None:
        return override(
            part, vision_model, vision_upstream, event_context=event_context
        )
    key = cache_key(part, vision_model)
    cached = cache_lookup(key)
    if cached is not None:
        _write_event(
            event_context,
            "image_proxy_cache_hit",
            vision_model=_canonical_model_id(vision_model),
        )
        return cached
    description = call_vision_model_for_description(
        part, vision_model, vision_upstream, event_context
    )
    if not isinstance(description, str) or not description.strip():
        raise ImageProxyError("Vision model returned no image description")
    cache_store(key, vision_model, description)
    return description


def image_reference(part: Mapping[str, Any], vision_model: str) -> str:
    return f"codexhub://image/{cache_key(part, vision_model)}"


def _description_text(description: str, image_path: str) -> str:
    safe_description = description.replace("</image>", "</ image>")
    return f'''The Gateway has already read the user's attached image. Use the visual context below as the image content when answering. Do not mention the Gateway, preprocessing, replacement, missing images, or inability to view the original attachment. Answer directly.\n\nVisual context:\n<image path="{image_path}">\n{safe_description}\n</image>'''


def _replace_image_parts(
    value: Any,
    describe: Callable[[Mapping[str, Any]], tuple[str, str]],
    *,
    protocol: RouteProtocol,
) -> tuple[Any, bool]:
    if is_image_part(value):
        description, image_path = describe(value)
        part_type = (
            "text" if protocol is RouteProtocol.CHAT_COMPLETIONS else "input_text"
        )
        return (
            {"type": part_type, "text": _description_text(description, image_path)},
            True,
        )
    if isinstance(value, list):
        changed = False
        output = []
        for item in value:
            replacement, item_changed = _replace_image_parts(
                item, describe, protocol=protocol
            )
            changed = changed or item_changed
            output.append(replacement)
        return (output, changed)
    if isinstance(value, dict):
        changed = False
        output = dict(value)
        for key, item in value.items():
            replacement, item_changed = _replace_image_parts(
                item, describe, protocol=protocol
            )
            if item_changed:
                output[key] = replacement
                changed = True
        return (output, changed)
    return (value, False)


def vision_upstream() -> tuple[str, Mapping[str, Any]]:
    if _vision_upstream_override is not None:
        return _vision_upstream_override()
    vision_model = _vision_model_reader()
    if not vision_model:
        raise ImageProxyError("Vision model is not configured for Vision Proxy")
    try:
        upstream = _resolve_upstream(vision_model)
    except ValueError as exc:
        raise ImageProxyError(
            f"Vision model is not available: {vision_model}: {exc}"
        ) from exc
    if not _model_supports_image(vision_model, upstream):
        raise ImageProxyError(
            f"Vision model does not support image input: {vision_model}"
        )
    return (vision_model, upstream)


def _apply_payload(
    payload: dict[str, Any],
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    *,
    protocol: RouteProtocol,
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
    image_proxy_enabled: bool | None = None,
    target_accepts_images: bool | None = None,
) -> bool:
    enabled = _enabled_reader() if image_proxy_enabled is None else image_proxy_enabled
    if not enabled:
        return False
    if target_accepts_images is None:
        target_accepts_images = bool(
            target_model and _model_supports_image(target_model, target_upstream)
        )
    if target_accepts_images:
        return False
    root_key = "messages" if protocol is RouteProtocol.CHAT_COMPLETIONS else "input"
    if not value_contains_image(payload.get(root_key)):
        return False
    vision_model, upstream = vision_upstream()
    descriptions: dict[str, str] = {}
    progress_sent = False
    image_count = unique_image_count(payload.get(root_key), vision_model)

    def emit_progress_once() -> bool:
        nonlocal progress_sent
        if progress_sent or progress_callback is None:
            return True
        if not progress_callback(
            {
                "type": "image_proxy",
                "status": "reading",
                "image_count": image_count,
                "vision_model": _canonical_model_id(vision_model),
            }
        ):
            return False
        progress_sent = True
        return True

    def describe(part: Mapping[str, Any]) -> tuple[str, str]:
        key = cache_key(part, vision_model)
        if key not in descriptions:
            if cache_lookup(key) is None and (not emit_progress_once()):
                raise _facts().downstream_closed_error(
                    "downstream closed during Vision Proxy"
                )
            descriptions[key] = description_for_part(
                part, vision_model, upstream, event_context=event_context
            )
        return (descriptions[key], image_reference(part, vision_model))

    replacement, changed = _replace_image_parts(
        payload.get(root_key), describe, protocol=protocol
    )
    if changed:
        payload[root_key] = replacement
        _write_event(
            event_context,
            "image_proxy_applied",
            vision_model=_canonical_model_id(vision_model),
            target_model=_canonical_model_id(target_model) if target_model else None,
            image_count=len(descriptions),
        )
    return changed


def apply_responses_payload(
    payload: dict[str, Any],
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
    *,
    image_proxy_enabled: bool | None = None,
    target_accepts_images: bool | None = None,
) -> bool:
    if _apply_responses_override is not None:
        return _apply_responses_override(
            payload,
            target_model,
            target_upstream,
            event_context=event_context,
            progress_callback=progress_callback,
            image_proxy_enabled=image_proxy_enabled,
            target_accepts_images=target_accepts_images,
        )
    return _apply_payload(
        payload,
        target_model,
        target_upstream,
        protocol=RouteProtocol.RESPONSES,
        event_context=event_context,
        progress_callback=progress_callback,
        image_proxy_enabled=image_proxy_enabled,
        target_accepts_images=target_accepts_images,
    )


def apply_chat_payload(
    payload: dict[str, Any],
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
    *,
    image_proxy_enabled: bool | None = None,
    target_accepts_images: bool | None = None,
) -> bool:
    if _apply_chat_override is not None:
        return _apply_chat_override(
            payload,
            target_model,
            target_upstream,
            event_context=event_context,
            progress_callback=progress_callback,
            image_proxy_enabled=image_proxy_enabled,
            target_accepts_images=target_accepts_images,
        )
    return _apply_payload(
        payload,
        target_model,
        target_upstream,
        protocol=RouteProtocol.CHAT_COMPLETIONS,
        event_context=event_context,
        progress_callback=progress_callback,
        image_proxy_enabled=image_proxy_enabled,
        target_accepts_images=target_accepts_images,
    )


def _context_with_policy(
    event_context: Mapping[str, Any] | None, policy: VisionProxyPolicy
) -> dict[str, Any] | None:
    if event_context is None:
        return None
    context = dict(event_context)
    context["vision_proxy_policy"] = policy.value
    return context


def apply(
    payload: dict[str, Any],
    *,
    inbound_protocol: RouteProtocol,
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    policy: VisionProxyPolicy,
    image_proxy_enabled: bool | None = None,
    target_accepts_images: bool | None = None,
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
) -> bool:
    if policy is VisionProxyPolicy.DISABLED:
        return False
    proxy_context = _context_with_policy(event_context, policy)
    if inbound_protocol is RouteProtocol.CHAT_COMPLETIONS:
        return apply_chat_payload(
            payload,
            target_model,
            target_upstream,
            event_context=proxy_context,
            progress_callback=progress_callback,
            image_proxy_enabled=image_proxy_enabled,
            target_accepts_images=target_accepts_images,
        )
    if inbound_protocol is RouteProtocol.RESPONSES:
        return apply_responses_payload(
            payload,
            target_model,
            target_upstream,
            event_context=proxy_context,
            progress_callback=progress_callback,
            image_proxy_enabled=image_proxy_enabled,
            target_accepts_images=target_accepts_images,
        )
    raise ImageProxyError("Vision Proxy received an unsupported inbound protocol")


def enforce_text_only_boundary(
    payload: dict[str, Any],
    *,
    inbound_protocol: RouteProtocol,
    target_model: str | None,
    target_upstream: Mapping[str, Any],
    vision_plan: VisionPlanLike,
    event_context: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
) -> bool:
    root_key = (
        "messages" if inbound_protocol is RouteProtocol.CHAT_COMPLETIONS else "input"
    )
    contains_image = value_contains_image(payload.get(root_key))
    if vision_plan.action is VisionAction.PASS_THROUGH:
        if contains_image and (not vision_plan.target_accepts_images):
            raise ImageProxyError(
                "Vision Proxy pass-through plan contradicts target image capability."
            )
        return False
    if vision_plan.action is VisionAction.REJECT:
        if not contains_image:
            return False
        model_label = (
            _canonical_model_id(target_model) if target_model else "the target model"
        )
        raise ImageProxyError(
            f"{model_label} does not support image input and Vision Proxy is disabled."
        )
    if vision_plan.network_action is not VisionNetworkAction.IMAGE_PROXY:
        raise ImageProxyError(
            "The planned Vision action has no executable network action."
        )
    try:
        policy = VisionProxyPolicy(vision_plan.policy)
    except ValueError as exc:
        raise ImageProxyError(
            "The planned Vision Proxy policy is unsupported."
        ) from exc
    if _boundary_override is not None:
        changed = _boundary_override(
            payload,
            inbound_format=inbound_protocol.value,
            target_model=target_model,
            target_upstream=target_upstream,
            vision_proxy_policy=vision_plan.policy,
            image_proxy_enabled=vision_plan.image_proxy_enabled,
            target_accepts_images=vision_plan.target_accepts_images,
            event_context=event_context,
            progress_callback=progress_callback,
        )
    else:
        changed = apply(
            payload,
            inbound_protocol=inbound_protocol,
            target_model=target_model,
            target_upstream=target_upstream,
            policy=policy,
            image_proxy_enabled=vision_plan.image_proxy_enabled,
            target_accepts_images=vision_plan.target_accepts_images,
            event_context=event_context,
            progress_callback=progress_callback,
        )
    root_key = (
        "messages" if inbound_protocol is RouteProtocol.CHAT_COMPLETIONS else "input"
    )
    remaining_image = value_contains_image(payload.get(root_key))
    if remaining_image:
        raise ImageProxyError(
            "Vision Proxy could not replace the image for the text-only target model."
        )
    changed = bool(changed) or (contains_image and (not remaining_image))
    if changed:
        _write_event(
            event_context,
            "image_proxy_boundary_guard_applied",
            target_model=_canonical_model_id(target_model) if target_model else None,
            inbound_format=inbound_protocol.value,
        )
    return changed


def _facts() -> VisionFacts:
    import gateway_stream_semantics

    return VisionFacts(
        cache_path=Path(IMAGE_PROXY_CACHE_PATH),
        cache_lock=IMAGE_PROXY_CACHE_LOCK,
        downstream_closed_error=gateway_stream_semantics.DownstreamClosedDuringImageProxyError,
    )


def _enabled_reader() -> bool:
    import gateway_settings

    return gateway_settings.gateway_image_proxy_enabled()


def _vision_model_reader() -> str:
    import gateway_settings

    return gateway_settings.gateway_image_proxy_model()


def _resolve_upstream(model: str) -> Mapping[str, Any]:
    import gateway_catalog_runtime

    return gateway_catalog_runtime.choose_upstream(model)


def _model_supports_image(model: str, upstream: Mapping[str, Any]) -> bool:
    import gateway_catalog_runtime

    return gateway_catalog_runtime.model_supports_image(model, upstream)


def _canonical_model_id(model: str) -> str:
    from catalog import canonical_model_id

    return canonical_model_id(model)


def _compatible_request_body(*args: Any, **kwargs: Any) -> bytes:
    import gateway_compat

    return gateway_compat.compatible_request_body(*args, **kwargs)


def _strip_tools(*args: Any, **kwargs: Any) -> bool:
    import gateway_stream_semantics

    return gateway_stream_semantics._strip_tools_for_text_only_proxy_payload(
        *args, **kwargs
    )


def _responses_url(*args: Any, **kwargs: Any) -> str:
    import route_plan

    return route_plan._responses_url(*args, **kwargs)


def _chat_completions_url(*args: Any, **kwargs: Any) -> str:
    import route_plan

    return route_plan._chat_completions_url(*args, **kwargs)


def _prepare_exchange(*args: Any, **kwargs: Any) -> Any:
    from protocol_translation import prepare_exchange

    return prepare_exchange(*args, **kwargs)


def _upstream_headers(*args: Any, **kwargs: Any) -> dict[str, str]:
    import gateway_transport

    return gateway_transport.upstream_headers(*args, **kwargs)


def _open_upstream(*args: Any, **kwargs: Any) -> Any:
    import gateway_transport

    return gateway_transport.open_upstream_response(*args, **kwargs)


def _upstream_timeout_seconds() -> int:
    import gateway_settings

    return gateway_settings.upstream_timeout_seconds()


def _response_is_event_stream(response: Any) -> bool:
    import gateway_request

    return gateway_request._is_event_stream(response)


def _events_to_responses_body(events: list[Mapping[str, Any]]) -> bytes:
    import gateway_stream_semantics

    return gateway_stream_semantics._events_to_responses_body(events)


def _write_event(
    event_context: Mapping[str, Any] | None, event: str, **fields: Any
) -> None:
    import gateway_events

    gateway_events.write_adapter_event(event_context, event, **fields)


def _usage_from_payload(payload: Any) -> Mapping[str, Any] | None:
    import gateway_events

    return gateway_events._usage_from_payload(payload)


def _normalize_usage(usage: Mapping[str, Any] | None) -> Mapping[str, Any]:
    import gateway_events

    return gateway_events.normalize_usage_for_event(usage)


def _safe_upstream_error_detail(error: BaseException) -> str:
    from gateway_errors import safe_upstream_error_detail

    return safe_upstream_error_detail(error)


_monotonic = time.monotonic
_wall_time = time.time
_cache_lookup_override = None
_cache_store_override = None
_response_body_override = None
_response_text_override = None
_describe_image_override = None
_description_for_part_override = None
_vision_upstream_override = None
_apply_responses_override = None
_apply_chat_override = None
_boundary_override = None


def _sse_assembler_factory() -> SseAssemblerLike:
    from sse_events import SseEventAssembler

    return SseEventAssembler()


def _response_event_payload(event: SseFrameLike) -> Mapping[str, Any] | str | None:
    import gateway_stream_semantics

    return gateway_stream_semantics._converted_sse_payload(event)
