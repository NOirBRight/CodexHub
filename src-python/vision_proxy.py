"""Typed Vision Proxy request adapter for the Python Gateway.

This module owns the Vision Proxy seam: image detection and fail-closed boundary
checks, persistent description caching, image-model requests, response text
extraction, and Responses/Chat Completions payload replacement.  Catalog and
runtime resolution, protocol codecs, transport, telemetry, and downstream
orchestration enter through typed hooks so this module remains independent of
the Gateway facade and HTTP handler.
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

from gateway_interfaces import AdapterEventWriter, UpstreamResponseLike
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


logger = logging.getLogger("vision_proxy")
IMAGE_PROXY_CACHE_LOCK = threading.Lock()


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


@dataclass(frozen=True)
class VisionProxyHooks:
    """Typed live dependencies supplied by Gateway orchestration."""

    enabled_reader: Callable[[], bool] = _false
    vision_model_reader: Callable[[], str] = _empty_text
    resolve_upstream: Callable[[str], Mapping[str, Any]] = _unbound_upstream
    model_supports_image: Callable[[str, Mapping[str, Any]], bool] = _unsupported_model
    canonical_model_id: Callable[[str], str] = _identity_model
    compatible_request_body: RequestCompatibilityHook = _unbound_request_compatibility
    strip_tools: ToolStripHook = _no_tools
    responses_url: Callable[..., str] = _unbound_url
    chat_completions_url: Callable[[Mapping[str, Any]], str] = _unbound_url
    prepare_exchange: PrepareExchangeHook = _unbound_prepare_exchange
    upstream_headers: Callable[[Mapping[str, str], Mapping[str, Any]], dict[str, str]] = _unbound_headers
    open_upstream: OpenUpstreamHook = _unbound_open
    upstream_timeout_seconds: Callable[[], int] = _default_timeout
    response_is_event_stream: Callable[[Any], bool] = _not_event_stream
    events_to_responses_body: Callable[[list[Mapping[str, Any]]], bytes] = _unbound_events_to_body
    write_event: AdapterEventWriter = _noop_event
    usage_from_payload: Callable[[Any], Mapping[str, Any] | None] = _empty_usage
    normalize_usage: Callable[[Mapping[str, Any] | None], Mapping[str, Any]] = _missing_usage
    safe_upstream_error_detail: Callable[[BaseException], str] = _safe_error
    monotonic: Callable[[], float] = time.monotonic
    wall_time: Callable[[], float] = time.time
    cache_lookup_override: Callable[[str], str | None] | None = None
    cache_store_override: Callable[[str, str, str], None] | None = None
    response_body_override: Callable[[Any], bytes] | None = None
    response_text_override: Callable[[Any], str] | None = None
    describe_image_override: Callable[..., str] | None = None
    description_for_part_override: Callable[..., str] | None = None
    vision_upstream_override: Callable[[], tuple[str, Mapping[str, Any]]] | None = None
    apply_responses_override: Callable[..., bool] | None = None
    apply_chat_override: Callable[..., bool] | None = None
    sse_assembler_factory: Callable[[], SseAssemblerLike] | None = None
    response_event_payload: ResponseEventPayloadHook | None = None
    boundary_override: BoundaryOverrideHook | None = None


@dataclass(frozen=True)
class VisionProxyAdapter:
    """Typed Vision Proxy seam used by the facade and direct tests."""

    facts: VisionFacts = field(default_factory=VisionFacts)
    hooks: VisionProxyHooks = field(default_factory=VisionProxyHooks)

    @staticmethod
    def is_image_part(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        part_type = value.get("type")
        if part_type == "input_image":
            return any(
                isinstance(value.get(key), str) and bool(value.get(key))
                for key in ("image_url", "file_id")
            )
        if part_type == "image_url":
            image_url = value.get("image_url")
            return (
                isinstance(image_url, Mapping)
                and isinstance(image_url.get("url"), str)
                and bool(image_url.get("url"))
            )
        return False

    def value_contains_image(self, value: Any) -> bool:
        if self.is_image_part(value):
            return True
        if isinstance(value, list):
            return any(self.value_contains_image(item) for item in value)
        if isinstance(value, Mapping):
            return any(self.value_contains_image(item) for item in value.values())
        return False

    @staticmethod
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

    def cache_key(self, part: Mapping[str, Any], vision_model: str) -> str:
        raw = json.dumps(
            {
                "image": self.normalized_image_part(part),
                "vision_model": vision_model,
                "prompt_version": self.facts.prompt_version,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def unique_image_count(self, value: Any, vision_model: str) -> int:
        cache_keys: set[str] = set()

        def collect(item: Any) -> None:
            if self.is_image_part(item):
                cache_keys.add(self.cache_key(item, vision_model))
            elif isinstance(item, list):
                for child in item:
                    collect(child)
            elif isinstance(item, Mapping):
                for child in item.values():
                    collect(child)

        collect(value)
        return len(cache_keys)

    @staticmethod
    def _ensure_cache(conn: sqlite3.Connection) -> None:
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

    def cache_lookup(self, cache_key: str) -> str | None:
        if self.hooks.cache_lookup_override is not None:
            return self.hooks.cache_lookup_override(cache_key)
        path = Path(self.facts.cache_path)
        try:
            with self.facts.cache_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(path)
                try:
                    self._ensure_cache(conn)
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

    def cache_store(self, cache_key: str, vision_model: str, description: str) -> None:
        if self.hooks.cache_store_override is not None:
            self.hooks.cache_store_override(cache_key, vision_model, description)
            return
        path = Path(self.facts.cache_path)
        try:
            with self.facts.cache_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(path)
                try:
                    self._ensure_cache(conn)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO image_proxy_cache
                        (cache_key, vision_model, prompt_version, description, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            cache_key,
                            vision_model,
                            self.facts.prompt_version,
                            description,
                            int(self.hooks.wall_time()),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except (OSError, sqlite3.DatabaseError) as exc:
            logger.warning("Vision Proxy cache store failed: %s", type(exc).__name__)

    @staticmethod
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
        return "\n".join(part.strip() for part in text_parts if part.strip()).strip()

    @staticmethod
    def _event_payload(event: SseFrameLike) -> Mapping[str, Any] | str | None:
        if not any(line.name == b"data" for line in event.lines) or not event.data:
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

    def response_body(self, response: Any) -> bytes:
        if self.hooks.response_body_override is not None:
            return self.hooks.response_body_override(response)
        if self.hooks.response_is_event_stream(response.headers):
            events: list[Mapping[str, Any]] = []
            if self.hooks.sse_assembler_factory is None:
                raise ImageProxyError("Vision Proxy SSE parser is not configured")
            assembler = self.hooks.sse_assembler_factory()
            event_payload = self.hooks.response_event_payload or self._event_payload
            while True:
                chunk = response.readline()
                if not chunk:
                    break
                for frame in assembler.feed(chunk):
                    payload = event_payload(frame)
                    if payload == "[DONE]" or payload is None:
                        continue
                    if not isinstance(payload, Mapping):
                        raise ImageProxyError("Vision model returned an unsupported SSE payload")
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
                    raise ImageProxyError("Vision model returned an unsupported SSE payload")
                events.append(payload)
            return self.hooks.events_to_responses_body(events)

        chunks: list[bytes] = []
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def call_vision_model_for_description(
        self,
        part: Mapping[str, Any],
        vision_model: str,
        vision_upstream: Mapping[str, Any],
        event_context: Mapping[str, Any] | None = None,
    ) -> str:
        if self.hooks.describe_image_override is not None:
            return self.hooks.describe_image_override(
                part,
                vision_model,
                vision_upstream,
                event_context,
            )
        started_at = self.hooks.monotonic()
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
                        {"type": "input_text", "text": self.facts.prompt},
                        self.normalized_image_part(part),
                    ],
                }
            ],
            "stream": upstream_format is RouteProtocol.RESPONSES,
        }
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        vision_context = dict(event_context or {})
        vision_context["image_proxy"] = True
        vision_context["vision_model"] = self.hooks.canonical_model_id(vision_model)
        try:
            body = self.hooks.compatible_request_body(
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
            if isinstance(vision_payload, dict) and self.hooks.strip_tools(
                vision_payload,
                event_context=vision_context,
                upstream_name=upstream_name,
                event_name="image_proxy_vision_tools_stripped",
            ):
                body = json.dumps(
                    vision_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            if upstream_format is RouteProtocol.CHAT_COMPLETIONS:
                exchange = self.hooks.prepare_exchange(
                    body,
                    inbound_format=RouteProtocol.RESPONSES.value,
                    outbound_format=RouteProtocol.CHAT_COMPLETIONS.value,
                )
                body = exchange.upstream_body
                upstream_url = self.hooks.chat_completions_url(vision_upstream)
            else:
                upstream_url = self.hooks.responses_url(vision_upstream, "/v1/responses")
            headers = self.hooks.upstream_headers(
                {"Content-Type": "application/json"},
                vision_upstream,
            )
        except ValueError as exc:
            raise ImageProxyError(f"Vision model request is invalid: {exc}") from exc

        request = Request(upstream_url, data=body, headers=headers, method="POST")
        vision_upstream_name = str(vision_upstream.get("name", "unknown"))
        self.hooks.write_event(
            event_context,
            "image_proxy_vision_request_start",
            vision_model=self.hooks.canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format.value,
            stream=payload["stream"],
        )
        try:
            with self.hooks.open_upstream(
                request,
                upstream_name=vision_upstream_name,
                upstream_format=upstream_format.value,
                timeout=self.hooks.upstream_timeout_seconds(),
                event_context=vision_context,
                request_kind=RETRY_REQUEST_IMAGE_PROXY_VISION,
                max_attempts=1,
            ) as response:
                response_status = getattr(response, "status", None)
                response_body = self.response_body(response)
        except BaseException as exc:
            self.hooks.write_event(
                event_context,
                "image_proxy_vision_request_error",
                vision_model=self.hooks.canonical_model_id(vision_model),
                upstream=vision_upstream_name,
                upstream_format=upstream_format.value,
                duration_ms=int((self.hooks.monotonic() - started_at) * 1000),
                error=type(exc).__name__,
                detail=self.hooks.safe_upstream_error_detail(exc),
            )
            raise

        try:
            response_payload = json.loads(response_body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.hooks.write_event(
                event_context,
                "image_proxy_vision_request_error",
                vision_model=self.hooks.canonical_model_id(vision_model),
                upstream=vision_upstream_name,
                upstream_format=upstream_format.value,
                duration_ms=int((self.hooks.monotonic() - started_at) * 1000),
                status=response_status if isinstance(response_status, int) else None,
                error=type(exc).__name__,
                detail="Vision model returned an invalid response",
            )
            raise ImageProxyError("Vision model returned an invalid response") from exc
        description = (
            self.hooks.response_text_override(response_payload)
            if self.hooks.response_text_override is not None
            else self.extract_response_text(response_payload)
        )
        if not description:
            self.hooks.write_event(
                event_context,
                "image_proxy_vision_request_error",
                vision_model=self.hooks.canonical_model_id(vision_model),
                upstream=vision_upstream_name,
                upstream_format=upstream_format.value,
                duration_ms=int((self.hooks.monotonic() - started_at) * 1000),
                status=response_status if isinstance(response_status, int) else None,
                error="EmptyImageDescription",
                detail="Vision model returned no image description",
                **dict(
                    self.hooks.normalize_usage(
                        self.hooks.usage_from_payload(response_payload)
                    )
                ),
            )
            raise ImageProxyError("Vision model returned no image description")
        self.hooks.write_event(
            event_context,
            "image_proxy_vision_request_complete",
            vision_model=self.hooks.canonical_model_id(vision_model),
            upstream=vision_upstream_name,
            upstream_format=upstream_format.value,
            duration_ms=int((self.hooks.monotonic() - started_at) * 1000),
            status=response_status if isinstance(response_status, int) else None,
            description_length=len(description),
            **dict(
                self.hooks.normalize_usage(
                    self.hooks.usage_from_payload(response_payload)
                )
            ),
        )
        return description

    def description_for_part(
        self,
        part: Mapping[str, Any],
        vision_model: str,
        vision_upstream: Mapping[str, Any],
        event_context: Mapping[str, Any] | None = None,
    ) -> str:
        if self.hooks.description_for_part_override is not None:
            return self.hooks.description_for_part_override(
                part,
                vision_model,
                vision_upstream,
                event_context=event_context,
            )
        cache_key = self.cache_key(part, vision_model)
        cached = self.cache_lookup(cache_key)
        if cached is not None:
            self.hooks.write_event(
                event_context,
                "image_proxy_cache_hit",
                vision_model=self.hooks.canonical_model_id(vision_model),
            )
            return cached
        description = self.call_vision_model_for_description(
            part,
            vision_model,
            vision_upstream,
            event_context,
        )
        if not isinstance(description, str) or not description.strip():
            raise ImageProxyError("Vision model returned no image description")
        self.cache_store(cache_key, vision_model, description)
        return description

    def image_reference(self, part: Mapping[str, Any], vision_model: str) -> str:
        return f"codexhub://image/{self.cache_key(part, vision_model)}"

    @staticmethod
    def _description_text(description: str, image_path: str) -> str:
        safe_description = description.replace("</image>", "</ image>")
        return (
            "The Gateway has already read the user's attached image. "
            "Use the visual context below as the image content when answering. "
            "Do not mention the Gateway, preprocessing, replacement, missing images, "
            "or inability to view the original attachment. Answer directly.\n\n"
            f'Visual context:\n<image path="{image_path}">\n{safe_description}\n</image>'
        )

    def _replace_image_parts(
        self,
        value: Any,
        describe: Callable[[Mapping[str, Any]], tuple[str, str]],
        *,
        protocol: RouteProtocol,
    ) -> tuple[Any, bool]:
        if self.is_image_part(value):
            description, image_path = describe(value)
            part_type = (
                "text"
                if protocol is RouteProtocol.CHAT_COMPLETIONS
                else "input_text"
            )
            return {
                "type": part_type,
                "text": self._description_text(description, image_path),
            }, True
        if isinstance(value, list):
            changed = False
            output = []
            for item in value:
                replacement, item_changed = self._replace_image_parts(
                    item,
                    describe,
                    protocol=protocol,
                )
                changed = changed or item_changed
                output.append(replacement)
            return output, changed
        if isinstance(value, dict):
            changed = False
            output = dict(value)
            for key, item in value.items():
                replacement, item_changed = self._replace_image_parts(
                    item,
                    describe,
                    protocol=protocol,
                )
                if item_changed:
                    output[key] = replacement
                    changed = True
            return output, changed
        return value, False

    def vision_upstream(self) -> tuple[str, Mapping[str, Any]]:
        if self.hooks.vision_upstream_override is not None:
            return self.hooks.vision_upstream_override()
        vision_model = self.hooks.vision_model_reader()
        if not vision_model:
            raise ImageProxyError("Vision model is not configured for Vision Proxy")
        try:
            vision_upstream = self.hooks.resolve_upstream(vision_model)
        except ValueError as exc:
            raise ImageProxyError(
                f"Vision model is not available: {vision_model}: {exc}"
            ) from exc
        if not self.hooks.model_supports_image(vision_model, vision_upstream):
            raise ImageProxyError(
                f"Vision model does not support image input: {vision_model}"
            )
        return vision_model, vision_upstream

    def _apply_payload(
        self,
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
        enabled = (
            self.hooks.enabled_reader()
            if image_proxy_enabled is None
            else image_proxy_enabled
        )
        if not enabled:
            return False
        if target_accepts_images is None:
            target_accepts_images = bool(
                target_model
                and self.hooks.model_supports_image(target_model, target_upstream)
            )
        if target_accepts_images:
            return False
        root_key = (
            "messages"
            if protocol is RouteProtocol.CHAT_COMPLETIONS
            else "input"
        )
        if not self.value_contains_image(payload.get(root_key)):
            return False

        vision_model, vision_upstream = self.vision_upstream()
        descriptions: dict[str, str] = {}
        progress_sent = False
        image_count = self.unique_image_count(payload.get(root_key), vision_model)

        def emit_progress_once() -> bool:
            nonlocal progress_sent
            if progress_sent or progress_callback is None:
                return True
            if not progress_callback(
                {
                    "type": "image_proxy",
                    "status": "reading",
                    "image_count": image_count,
                    "vision_model": self.hooks.canonical_model_id(vision_model),
                }
            ):
                return False
            progress_sent = True
            return True

        def describe(part: Mapping[str, Any]) -> tuple[str, str]:
            cache_key = self.cache_key(part, vision_model)
            if cache_key not in descriptions:
                if self.cache_lookup(cache_key) is None and not emit_progress_once():
                    raise self.facts.downstream_closed_error(
                        "downstream closed during Vision Proxy"
                    )
                descriptions[cache_key] = self.description_for_part(
                    part,
                    vision_model,
                    vision_upstream,
                    event_context=event_context,
                )
            return descriptions[cache_key], self.image_reference(part, vision_model)

        replacement, changed = self._replace_image_parts(
            payload.get(root_key),
            describe,
            protocol=protocol,
        )
        if changed:
            payload[root_key] = replacement
            self.hooks.write_event(
                event_context,
                "image_proxy_applied",
                vision_model=self.hooks.canonical_model_id(vision_model),
                target_model=(
                    self.hooks.canonical_model_id(target_model)
                    if target_model
                    else None
                ),
                image_count=len(descriptions),
            )
        return changed

    def apply_responses_payload(
        self,
        payload: dict[str, Any],
        target_model: str | None,
        target_upstream: Mapping[str, Any],
        event_context: Mapping[str, Any] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
        *,
        image_proxy_enabled: bool | None = None,
        target_accepts_images: bool | None = None,
    ) -> bool:
        if self.hooks.apply_responses_override is not None:
            return self.hooks.apply_responses_override(
                payload,
                target_model,
                target_upstream,
                event_context=event_context,
                progress_callback=progress_callback,
                image_proxy_enabled=image_proxy_enabled,
                target_accepts_images=target_accepts_images,
            )
        return self._apply_payload(
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
        self,
        payload: dict[str, Any],
        target_model: str | None,
        target_upstream: Mapping[str, Any],
        event_context: Mapping[str, Any] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
        *,
        image_proxy_enabled: bool | None = None,
        target_accepts_images: bool | None = None,
    ) -> bool:
        if self.hooks.apply_chat_override is not None:
            return self.hooks.apply_chat_override(
                payload,
                target_model,
                target_upstream,
                event_context=event_context,
                progress_callback=progress_callback,
                image_proxy_enabled=image_proxy_enabled,
                target_accepts_images=target_accepts_images,
            )
        return self._apply_payload(
            payload,
            target_model,
            target_upstream,
            protocol=RouteProtocol.CHAT_COMPLETIONS,
            event_context=event_context,
            progress_callback=progress_callback,
            image_proxy_enabled=image_proxy_enabled,
            target_accepts_images=target_accepts_images,
        )

    @staticmethod
    def _context_with_policy(
        event_context: Mapping[str, Any] | None,
        policy: VisionProxyPolicy,
    ) -> dict[str, Any] | None:
        if event_context is None:
            return None
        context = dict(event_context)
        context["vision_proxy_policy"] = policy.value
        return context

    def apply(
        self,
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
        proxy_context = self._context_with_policy(event_context, policy)
        if inbound_protocol is RouteProtocol.CHAT_COMPLETIONS:
            return self.apply_chat_payload(
                payload,
                target_model,
                target_upstream,
                event_context=proxy_context,
                progress_callback=progress_callback,
                image_proxy_enabled=image_proxy_enabled,
                target_accepts_images=target_accepts_images,
            )
        if inbound_protocol is RouteProtocol.RESPONSES:
            return self.apply_responses_payload(
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
        self,
        payload: dict[str, Any],
        *,
        inbound_protocol: RouteProtocol,
        target_model: str | None,
        target_upstream: Mapping[str, Any],
        vision_plan: VisionPlanLike,
        event_context: Mapping[str, Any] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> bool:
        root_key = "messages" if inbound_protocol is RouteProtocol.CHAT_COMPLETIONS else "input"
        contains_image = self.value_contains_image(payload.get(root_key))
        if vision_plan.action is VisionAction.PASS_THROUGH:
            if contains_image and not vision_plan.target_accepts_images:
                raise ImageProxyError("Vision Proxy pass-through plan contradicts target image capability.")
            return False
        if vision_plan.action is VisionAction.REJECT:
            if not contains_image:
                return False
            model_label = (
                self.hooks.canonical_model_id(target_model)
                if target_model
                else "the target model"
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
            raise ImageProxyError("The planned Vision Proxy policy is unsupported.") from exc

        if self.hooks.boundary_override is not None:
            changed = self.hooks.boundary_override(
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
            changed = self.apply(
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
            "messages"
            if inbound_protocol is RouteProtocol.CHAT_COMPLETIONS
            else "input"
        )
        remaining_image = self.value_contains_image(payload.get(root_key))
        if remaining_image:
            raise ImageProxyError(
                "Vision Proxy could not replace the image for the text-only target model."
            )
        changed = bool(changed) or (contains_image and not remaining_image)
        if changed:
            self.hooks.write_event(
                event_context,
                "image_proxy_boundary_guard_applied",
                target_model=(
                    self.hooks.canonical_model_id(target_model)
                    if target_model
                    else None
                ),
                inbound_format=inbound_protocol.value,
            )
        return changed
