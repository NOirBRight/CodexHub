"""ChatGPT Web session binding and loopback route lookup.

Gateway still owns the public Responses exchange. This module only resolves
the supervised runtime, projects models from explicit account checks, and keeps the caller's
session, thread, turn, and item identities intact.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from catalog import canonical_model_id, compose_flat_label
from gateway_errors import identity_failure

import chatgpt_web_collab
import chatgpt_web_connection
import chatgpt_web_runtime

PROVIDER_ID = "chatgpt-web"
UPSTREAM_NAME = "chatgpt_web"
DISPLAY_PREFIX = "Web"
LOOPBACK_HOST = "127.0.0.1"

REASON_NOT_READY = "chatgpt_web_not_ready"
REASON_MODEL_NOT_LISTED = "chatgpt_web_model_not_listed"
REASON_MODEL_NOT_ENABLED = "chatgpt_web_model_not_enabled"
REASON_PIN_INCOMPATIBLE = "chatgpt_web_pin_incompatible"
REASON_PROCESS_NOT_RUNNING = "chatgpt_web_process_not_running"
REASON_BROWSER_SMOKE_FAILED = "chatgpt_web_browser_smoke_failed"
REASON_TUNNEL_NOT_READY = "chatgpt_web_tunnel_not_ready"
REASON_CONNECTOR_NOT_SELECTABLE = "chatgpt_web_connector_not_selectable"
REASON_TOOL_RUNTIME_NOT_READY = "chatgpt_web_tool_runtime_not_ready"
REASON_TOOL_CALL_REJECTED = "chatgpt_web_tool_call_rejected"
REASON_IMAGE_INPUT_UNLISTED = "chatgpt_web_image_input_unlisted"
REASON_ALREADY_SUBMITTED = "chatgpt_web_already_submitted"
_COMPACT_KINDS = frozenset({"compact", "compaction"})
_TOOL_ITEM_TYPES = frozenset({"function_call", "function_call_output"})
_PRESERVE_IF_PRESENT = ("tools", "tool_choice", "environment", "sandbox")

_EFFORT_DESCRIPTIONS = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
    "max": "Maximum upstream reasoning depth",
}


def is_web_slug(slug: str) -> bool:
    identity = canonical_model_id(slug)
    return identity == PROVIDER_ID or identity.startswith(f"{PROVIDER_ID}/")


def read_status(home: Any | None = None) -> dict[str, Any]:
    runtime_home = chatgpt_web_runtime.default_home() if home is None else home
    status = chatgpt_web_runtime.build_status(runtime_home)
    return status if isinstance(status, dict) else {}


def listed_models(status: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], ...]:
    payload = read_status() if status is None else status
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return ()
    models: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not is_web_slug(model_id):
            continue
        efforts = item.get("efforts")
        parsed_efforts = tuple(
            effort.strip().lower()
            for effort in efforts
            if isinstance(effort, str) and effort.strip()
        ) if isinstance(efforts, list) else ()
        display_name = item.get("display_name")
        models.append(
            {
                "id": canonical_model_id(model_id),
                "display_name": display_name.strip() if isinstance(display_name, str) else "",
                "efforts": parsed_efforts,
                "image_input": item.get("image_input") is True,
            }
        )
    return tuple(models)


def project_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Append only account-visible runtime models enabled in the ChatGPT Provider."""
    try:
        status = read_status()
        enabled_models = chatgpt_web_connection.selected_model_ids(status)
    except Exception:
        return dict(catalog)
    if not _text_runtime_admitted(status):
        return dict(catalog)
    models = catalog.get("models")
    projected = [item for item in models if isinstance(item, Mapping)] if isinstance(models, list) else []
    seen = {
        canonical_model_id(str(item.get("slug") or ""))
        for item in projected
        if isinstance(item.get("slug"), str)
    }
    added = False
    for model in listed_models(status):
        if model["id"] not in enabled_models:
            continue
        if model["id"] in seen:
            continue
        projected.append(_catalog_entry(model))
        seen.add(model["id"])
        added = True
    if not added:
        return dict(catalog)
    updated = dict(catalog)
    updated["models"] = projected
    return updated


def upstream_for_model(slug: str) -> dict[str, Any]:
    """Return the exact loopback route, or raise before any runtime connection."""
    identity = canonical_model_id(slug)
    status = read_status()
    _raise_for_status(status, identity)
    selected = next((model for model in listed_models(status) if model["id"] == identity), None)
    if selected is None:
        raise identity_failure(
            f"ChatGPT Web model is not in the account-visible runtime model list: {identity}",
            reason=REASON_MODEL_NOT_LISTED,
            provider_id=PROVIDER_ID,
            model_slug=identity,
        )
    if identity not in chatgpt_web_connection.selected_model_ids(status):
        raise identity_failure(
            f"ChatGPT Web model is not enabled for Gateway: {identity}",
            reason=REASON_MODEL_NOT_ENABLED,
            provider_id=PROVIDER_ID,
            model_slug=identity,
        )
    return _upstream_facts(status, selected)


def ensure_exchange_allowed(upstream: Mapping[str, Any], payload: Mapping[str, Any] | None) -> None:
    """Re-check readiness and effort immediately before the upstream request is opened."""
    _consume_runtime_drain()
    identity = canonical_model_id(str(upstream.get("model_id") or upstream.get("upstream_model") or ""))
    status = read_status()
    sync_process_generation(status)
    _raise_for_status(status, identity)
    selected = next((model for model in listed_models(status) if model["id"] == identity), None)
    if selected is None:
        raise identity_failure(
            f"ChatGPT Web model is not in the account-visible runtime model list: {identity}",
            reason=REASON_MODEL_NOT_LISTED,
            provider_id=PROVIDER_ID,
            model_slug=identity,
        )
    if payload is not None:
        requested = _requested_effort(payload)
        if requested is not None and requested not in selected["efforts"]:
            raise identity_failure(
                f"ChatGPT Web effort is not in the model's reported effort list: {requested}",
                reason=REASON_MODEL_NOT_LISTED,
                provider_id=PROVIDER_ID,
                model_slug=identity,
            )
        _raise_if_image_unlisted(selected, payload, identity)
        if _request_needs_tools(payload):
            _raise_for_tool_runtime(status, identity)


def bind_responses_body(body: bytes) -> bytes:
    """Preserve caller call and item identities.

    Does not mint a turn, does not give every message one shared turn, and does
    not copy an item id into a turn id or the reverse. A missing identity stays
    missing so the runtime can fail the turn itself.
    """
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    bound = dict(payload)
    metadata = payload.get("client_metadata")
    if isinstance(metadata, dict):
        bound["client_metadata"] = json.loads(json.dumps(metadata))
    elif "client_metadata" in payload:
        bound["client_metadata"] = metadata
    if "prompt_cache_key" in payload:
        bound["prompt_cache_key"] = payload.get("prompt_cache_key")
    items = payload.get("input")
    if isinstance(items, list):
        bound_items: list[Any] = []
        for item in items:
            if not isinstance(item, dict):
                bound_items.append(item)
                continue
            copied = dict(item)
            if "id" in item:
                copied["id"] = item.get("id")
            passthrough = item.get("internal_chat_message_metadata_passthrough")
            if isinstance(passthrough, dict):
                copied["internal_chat_message_metadata_passthrough"] = dict(passthrough)
            elif "internal_chat_message_metadata_passthrough" in item:
                copied["internal_chat_message_metadata_passthrough"] = passthrough
            bound_items.append(copied)
        bound["input"] = bound_items
    if "model" in payload:
        bound["model"] = payload.get("model")
    if "reasoning" in payload:
        bound["reasoning"] = payload.get("reasoning")
    for key in _PRESERVE_IF_PRESENT:
        if key in payload:
            bound[key] = _json_copy(payload.get(key))
    apply_message_filter(bound)
    return json.dumps(bound, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _raise_for_status(status: Mapping[str, Any], slug: str) -> None:
    component = status.get("component")
    compatible = isinstance(component, Mapping) and component.get("compatible") is True
    if status.get("installed") is True and not compatible:
        raise identity_failure(
            "ChatGPT Web runtime pin is incompatible",
            reason=REASON_PIN_INCOMPATIBLE,
            provider_id=PROVIDER_ID,
            model_slug=slug,
        )
    process = status.get("process")
    running = isinstance(process, Mapping) and process.get("running") is True
    if not running:
        raise identity_failure(
            "ChatGPT Web runtime process is not running",
            reason=REASON_PROCESS_NOT_RUNNING,
            provider_id=PROVIDER_ID,
            model_slug=slug,
        )
    if not _text_runtime_admitted(status):
        login = status.get("login")
        if isinstance(login, Mapping) and login.get("state") == "signed_out":
            raise identity_failure(
                "ChatGPT Web runtime is signed out and not ready",
                reason=REASON_NOT_READY,
                provider_id=PROVIDER_ID,
                model_slug=slug,
            )
        if not isinstance(login, Mapping) or login.get("state") != "signed_in":
            raise identity_failure(
                "ChatGPT Web login has not been verified and the runtime is not ready",
                reason=REASON_NOT_READY,
                provider_id=PROVIDER_ID,
                model_slug=slug,
            )
        smoke = status.get("browser_smoke")
        if not isinstance(smoke, Mapping) or smoke.get("state") != "passed":
            raise identity_failure(
                "ChatGPT Web browser smoke failed",
                reason=REASON_BROWSER_SMOKE_FAILED,
                provider_id=PROVIDER_ID,
                model_slug=slug,
            )
        raise identity_failure(
            "ChatGPT Web runtime is not ready",
            reason=REASON_NOT_READY,
            provider_id=PROVIDER_ID,
            model_slug=slug,
        )


def _text_runtime_admitted(status: Mapping[str, Any]) -> bool:
    """Text turns need login and browser smoke, not tunnel or connector readiness.

    ``status["ready"]`` stays the provider-card flag for every layer. This
    admission does not read it.
    """
    component = status.get("component")
    if not (isinstance(component, Mapping) and component.get("compatible") is True):
        return False
    process = status.get("process")
    if not isinstance(process, Mapping) or process.get("running") is not True:
        return False
    port = process.get("port")
    if (
        process.get("listen_host") != LOOPBACK_HOST
        or not isinstance(port, int)
        or isinstance(port, bool)
        or port <= 0
    ):
        return False
    if status.get("disabled") is True or status.get("restart_required") is True:
        return False
    if status.get("admitting") is False:
        return False
    login = status.get("login")
    if not isinstance(login, Mapping) or login.get("state") != "signed_in":
        return False
    smoke = status.get("browser_smoke")
    checks = status.get("readiness_checks")
    return (
        isinstance(smoke, Mapping)
        and smoke.get("state") == "passed"
        and isinstance(checks, Mapping)
        and checks.get("capabilities_match") is True
    )


def _upstream_facts(status: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, Any]:
    try:
        base_url, token = chatgpt_web_connection.resolve_connection(
            *chatgpt_web_connection.provider_overrides(),
            home=chatgpt_web_runtime.default_home(),
            status=status,
        )
    except ValueError as exc:
        raise identity_failure(
            str(exc),
            reason=REASON_NOT_READY,
            provider_id=PROVIDER_ID,
            model_slug=str(model["id"]),
        ) from exc
    return {
        "name": UPSTREAM_NAME,
        "provider_id": PROVIDER_ID,
        "model_id": model["id"],
        "base_url": base_url,
        "auth": "api_key",
        "api_key": token,
        "upstream_model": model["id"],
        "upstream_format": "responses",
        # ``none`` keeps Gateway tool injection and structured rewriting off
        # (see gateway_compat request shaping and route_plan tool exposure).
        # This route already forwards Responses bodies unchanged, so caller
        # tool declarations, argument deltas, call ids, and item ids stay intact.
        "tool_protocol": "none",
        "tool_surface_strategy": "eager",
        "native_responses_tool_codec": "none",
        "reports_cached_input_tokens": False,
        "supports_developer_role": True,
        "supported_reasoning_levels": tuple(model["efforts"]),
        "input_modalities": _input_modalities(model),
    }


def _requested_effort(payload: Mapping[str, Any]) -> str | None:
    candidates = [payload.get("reasoning_effort")]
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, Mapping):
        candidates.append(reasoning.get("effort"))
    elif isinstance(reasoning, str):
        candidates.append(reasoning)
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _catalog_entry(model: Mapping[str, Any]) -> dict[str, Any]:
    short_name = str(model.get("display_name") or "").strip() or str(model["id"]).rsplit("/", 1)[-1]
    efforts = [
        {
            "effort": effort,
            "description": _EFFORT_DESCRIPTIONS.get(effort, f"{effort} reasoning effort"),
        }
        for effort in model["efforts"]
        if isinstance(effort, str) and effort
    ]
    entry: dict[str, Any] = {
        "slug": model["id"],
        "display_name": compose_flat_label(DISPLAY_PREFIX, short_name),
        "description": "Account-visible model from the managed ChatGPT Web runtime.",
        "visibility": "list",
        "supported_in_api": True,
        "input_modalities": list(_input_modalities(model)),
        "supported_reasoning_levels": efforts,
        "codex_proxy_metadata": {
            "provider": PROVIDER_ID,
            "upstream_name": UPSTREAM_NAME,
            "upstream_model": model["id"],
        },
    }
    if efforts:
        entry["default_reasoning_level"] = efforts[0]["effort"]
    return entry


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return value


def _input_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = payload.get("input")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _request_needs_tools(payload: Mapping[str, Any] | None) -> bool:
    """True when the caller declared tools or sent a function call/result."""
    if not isinstance(payload, Mapping):
        return False
    tools = payload.get("tools")
    if isinstance(tools, list) and any(isinstance(tool, Mapping) for tool in tools):
        return True
    return any(item.get("type") in _TOOL_ITEM_TYPES for item in _input_items(payload))


def _raise_for_tool_runtime(status: Mapping[str, Any], slug: str) -> None:
    tunnel = status.get("tunnel")
    connector = status.get("connector")
    tunnel_ready = isinstance(tunnel, Mapping) and tunnel.get("state") == "ready"
    connector_ready = isinstance(connector, Mapping) and connector.get("selectable") is True
    if tunnel_ready and connector_ready:
        return
    if not tunnel_ready and not connector_ready:
        message = "ChatGPT Web tool tunnel is not ready and tool connector is not selectable"
        reason = REASON_TOOL_RUNTIME_NOT_READY
    elif not tunnel_ready:
        message = "ChatGPT Web tool tunnel is not ready"
        reason = REASON_TUNNEL_NOT_READY
    else:
        message = "ChatGPT Web tool connector is not selectable"
        reason = REASON_CONNECTOR_NOT_SELECTABLE
    raise identity_failure(
        message,
        reason=reason,
        provider_id=PROVIDER_ID,
        model_slug=slug,
    )


def _caller_turn(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    metadata = payload.get("client_metadata")
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("x-codex-turn-metadata")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    thread_id = parsed.get("thread_id")
    turn_id = parsed.get("turn_id")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(turn_id, str) or not turn_id:
        return None
    cache_key = payload.get("prompt_cache_key")
    if isinstance(cache_key, str) and cache_key and cache_key != thread_id:
        return None
    return thread_id, turn_id


def _malformed_tool_output(payload: Mapping[str, Any]) -> bool:
    for item in _input_items(payload):
        if item.get("type") != "function_call_output":
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return True
    return False


def _output_call_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    call_ids: list[str] = []
    for item in _input_items(payload):
        if item.get("type") != "function_call_output":
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id and call_id not in call_ids:
            call_ids.append(call_id)
    return tuple(call_ids)


def _tool_failure(message: str, slug: str) -> None:
    raise identity_failure(
        message,
        reason=REASON_TOOL_CALL_REJECTED,
        provider_id=PROVIDER_ID,
        model_slug=slug,
    )


@dataclass
class _CallPermit:
    thread_id: str
    turn_id: str
    call_id: str
    item_id: str | None = None
    state: str = "open"
    response_body: bytes | None = None


@dataclass
class _Inflight:
    thread_id: str
    turn_id: str
    phase: str
    call_ids: tuple[str, ...] = ()
    suppress_keys: frozenset[str] = frozenset()
    forward_keys: frozenset[str] = frozenset()
    observed: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    submitted: bool = False
    terminal_seen: bool = False
    aborted: bool = False
    settled: bool = False


class _RecordedUpstreamResponse:
    """In-memory Responses SSE body. Replays a turn without a new upstream POST."""

    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.code = 200
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        lines = body.splitlines(keepends=True)
        if lines and not lines[-1].endswith(b"\n"):
            lines[-1] += b"\n"
        self._lines = lines or [b"\n"]
        self._index = 0
        self._closed = False

    def readline(self) -> bytes:
        if self._closed or self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line

    def read(self) -> bytes:
        if self._closed:
            return b""
        rest = b"".join(self._lines[self._index :])
        self._index = len(self._lines)
        return rest

    def close(self) -> None:
        self._closed = True
        self._index = len(self._lines)

    def getheader(self, name: str, default: str | None = None) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default


_TOOL_LOCK = threading.Lock()
_CALLS: dict[str, _CallPermit] = {}
_FORWARDED: dict[str, set[str]] = {}
_SUPPRESSED: dict[str, set[str]] = {}
_EPOCHS: dict[str, tuple[str, str | None]] = {}
_PROCESS_GENERATION: str | None = None
_BIND_FILTER = threading.local()


def recorded_upstream_response(body: bytes) -> _RecordedUpstreamResponse:
    return _RecordedUpstreamResponse(body)


def prepare_responses_exchange(
    upstream: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
    event_context: dict[str, Any] | None,
    admission: Any | None,
) -> bytes | None:
    """Admit a web turn. Return recorded SSE bytes when a tool output is replayed.

    A returned body must be relayed without opening ``/v1/responses``. ``None``
    means the caller should forward the bound request once.
    """
    clear_message_filter()
    ensure_exchange_allowed(upstream, payload)
    if not isinstance(payload, Mapping):
        return None
    slug = canonical_model_id(str(upstream.get("model_id") or upstream.get("upstream_model") or ""))
    identity = _caller_turn(payload)
    compaction = _is_compaction_request(payload, event_context)
    if identity is not None and not compaction:
        note_turn_epoch(identity[0], slug, _requested_effort(payload))
    suppress_keys: frozenset[str] = frozenset()
    forward_keys: frozenset[str] = frozenset()
    if identity is not None:
        suppress_keys, forward_keys = plan_message_filter(identity[0], payload, compaction=compaction)
    elif not compaction and not _request_needs_tools(payload):
        return None
    outputs: tuple[str, ...] = ()
    replay_body: bytes | None = None
    if not compaction and _request_needs_tools(payload):
        if _malformed_tool_output(payload):
            clear_message_filter()
            _tool_failure("ChatGPT Web tool call is unknown", slug)
        outputs = _output_call_ids(payload)
        if outputs and identity is None:
            clear_message_filter()
            _tool_failure("ChatGPT Web tool call cannot be bound to this turn", slug)
        if outputs and identity is not None:
            replay_body = _claim_tool_outputs(identity[0], identity[1], outputs, slug)
            if replay_body is not None:
                clear_message_filter()
                return replay_body
    if identity is None or not isinstance(event_context, dict):
        return None
    if compaction:
        phase = "compact"
    elif outputs:
        phase = "continue"
    elif _request_needs_tools(payload):
        phase = "issue"
    else:
        phase = "text"
    inflight = _Inflight(
        thread_id=identity[0],
        turn_id=identity[1],
        phase=phase,
        call_ids=outputs,
        suppress_keys=suppress_keys,
        forward_keys=forward_keys,
    )
    event_context["chatgpt_web_inflight"] = inflight
    if admission is not None:
        add_listener = getattr(admission, "add_cancel_listener", None)
        if callable(add_listener):
            add_listener(lambda: _abort_inflight(inflight))
    return None


def note_upstream_submitted(event_context: Mapping[str, Any] | None) -> None:
    inflight = _inflight_from(event_context)
    if inflight is None:
        return
    with _TOOL_LOCK:
        inflight.submitted = True
        if inflight.forward_keys:
            _FORWARDED.setdefault(inflight.thread_id, set()).update(inflight.forward_keys)


@contextmanager
def submission_guard(event_context: Mapping[str, Any] | None) -> Iterator[None]:
    try:
        yield
    finally:
        _settle_submission(event_context)


def observe_upstream_event(event: Mapping[str, Any], event_context: Mapping[str, Any] | None) -> None:
    """Record function-call identities from the runtime SSE without rewriting them."""
    inflight = _inflight_from(event_context)
    if inflight is None or not isinstance(event, Mapping):
        return
    try:
        copied = json.loads(json.dumps(event))
    except (TypeError, ValueError):
        return
    if not isinstance(copied, dict):
        return
    with _TOOL_LOCK:
        if inflight.phase == "issue":
            for call_id, item_id in _function_calls_in_event(copied):
                permit = _CALLS.get(call_id)
                if permit is None:
                    _CALLS[call_id] = _CallPermit(
                        thread_id=inflight.thread_id,
                        turn_id=inflight.turn_id,
                        call_id=call_id,
                        item_id=item_id,
                    )
                elif (
                    permit.state == "open"
                    and permit.thread_id == inflight.thread_id
                    and permit.turn_id == inflight.turn_id
                    and item_id
                    and not permit.item_id
                ):
                    permit.item_id = item_id
                inflight.observed.add(call_id)
        inflight.events.append(copied)
        if copied.get("type") != "response.completed":
            return
        inflight.terminal_seen = True
        if inflight.phase == "compact":
            if inflight.suppress_keys:
                _SUPPRESSED.setdefault(inflight.thread_id, set()).update(inflight.suppress_keys)
            return
        if inflight.phase != "continue":
            return
        body = _encode_sse(inflight.events)
        for call_id in inflight.call_ids:
            permit = _CALLS.get(call_id)
            if (
                permit is not None
                and permit.state == "forwarding"
                and permit.thread_id == inflight.thread_id
                and permit.turn_id == inflight.turn_id
            ):
                permit.state = "recorded"
                permit.response_body = body


def revoke_all_tool_permissions() -> None:
    """Drop tool-result permission for turns the Gateway can no longer finish."""
    with _TOOL_LOCK:
        for permit in _CALLS.values():
            if permit.state in {"open", "forwarding", "recorded"}:
                permit.state = "revoked"
                permit.response_body = None


_DRAIN_SEEN: str | None = None


def _consume_runtime_drain() -> None:
    """Revoke once when the supervisor records a version switch."""
    global _DRAIN_SEEN
    document = chatgpt_web_runtime._read_json(chatgpt_web_runtime.default_home() / "drain.json") or {}
    token = document.get("id")
    if not isinstance(token, str) or not token or token == _DRAIN_SEEN:
        return
    _DRAIN_SEEN = token
    revoke_all_tool_permissions()
    chatgpt_web_collab.revoke_all_permissions()
    import chatgpt_web_client_session

    chatgpt_web_client_session.revoke_all_tool_permissions()


def _claim_tool_outputs(thread_id: str, turn_id: str, call_ids: tuple[str, ...], slug: str) -> bytes | None:
    with _TOOL_LOCK:
        permits: list[_CallPermit] = []
        for call_id in call_ids:
            permit = _CALLS.get(call_id)
            if permit is None:
                _tool_failure("ChatGPT Web tool call is unknown", slug)
            assert permit is not None
            if permit.thread_id != thread_id:
                _tool_failure("ChatGPT Web tool call belongs to another session", slug)
            if permit.turn_id != turn_id:
                _tool_failure("ChatGPT Web tool call is not active on this turn", slug)
            if permit.state == "revoked":
                _tool_failure("ChatGPT Web tool call expired", slug)
            if permit.state == "forwarding":
                _tool_failure("ChatGPT Web tool call is already in flight", slug)
            if permit.state not in {"open", "recorded"}:
                _tool_failure("ChatGPT Web tool call expired", slug)
            permits.append(permit)
        if all(permit.state == "recorded" and permit.response_body for permit in permits):
            return permits[-1].response_body
        if any(permit.state != "open" for permit in permits):
            _tool_failure("ChatGPT Web tool call expired", slug)
        for permit in permits:
            permit.state = "forwarding"
            permit.response_body = None
    return None


def _abort_inflight(inflight: _Inflight) -> None:
    with _TOOL_LOCK:
        if inflight.terminal_seen or inflight.aborted:
            return
        inflight.aborted = True
        _revoke_inflight_locked(inflight)


def _settle_submission(event_context: Mapping[str, Any] | None) -> None:
    inflight = _inflight_from(event_context)
    if inflight is None:
        return
    with _TOOL_LOCK:
        if inflight.settled:
            return
        inflight.settled = True
        if inflight.terminal_seen or inflight.aborted:
            return
        if inflight.phase == "continue" and not inflight.submitted:
            for call_id in inflight.call_ids:
                permit = _CALLS.get(call_id)
                if (
                    permit is not None
                    and permit.state == "forwarding"
                    and permit.thread_id == inflight.thread_id
                    and permit.turn_id == inflight.turn_id
                ):
                    permit.state = "open"
            return
        _revoke_inflight_locked(inflight)


def _revoke_inflight_locked(inflight: _Inflight) -> None:
    call_ids = inflight.call_ids if inflight.phase == "continue" else tuple(inflight.observed)
    for call_id in call_ids:
        permit = _CALLS.get(call_id)
        if permit is None:
            continue
        if permit.thread_id != inflight.thread_id or permit.turn_id != inflight.turn_id:
            continue
        if permit.state in {"open", "forwarding"}:
            permit.state = "revoked"
            permit.response_body = None


def _inflight_from(event_context: Mapping[str, Any] | None) -> _Inflight | None:
    if not isinstance(event_context, Mapping):
        return None
    inflight = event_context.get("chatgpt_web_inflight")
    return inflight if isinstance(inflight, _Inflight) else None


def _function_calls_in_event(event: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []

    def remember(item: Any) -> None:
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            return
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        item_id = item.get("id")
        found.append((call_id, item_id if isinstance(item_id, str) and item_id else None))

    event_type = event.get("type")
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        remember(event.get("item"))
    elif event_type == "response.completed":
        response = event.get("response")
        output = response.get("output") if isinstance(response, Mapping) else None
        if isinstance(output, list):
            for item in output:
                remember(item)
    elif event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
        call_id = event.get("call_id")
        if isinstance(call_id, str) and call_id:
            item_id = event.get("item_id")
            found.append((call_id, item_id if isinstance(item_id, str) and item_id else None))
    return found


def _encode_sse(events: list[dict[str, Any]]) -> bytes:
    chunks = [
        b"data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
        for event in events
    ]
    return b"".join(chunks)


def _input_modalities(model: Mapping[str, Any]) -> tuple[str, ...]:
    if model.get("image_input") is True:
        return ("text", "image")
    return ("text",)


def _contains_image(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("type") in {"input_image", "image_url", "image"}:
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image(item) for item in value)
    return False


def _raise_if_image_unlisted(selected: Mapping[str, Any], payload: Mapping[str, Any] | None, identity: str) -> None:
    if not isinstance(payload, Mapping) or not _contains_image(payload):
        return
    if selected.get("image_input") is True:
        return
    raise identity_failure(
        "ChatGPT Web model does not list image input",
        reason=REASON_IMAGE_INPUT_UNLISTED,
        provider_id=PROVIDER_ID,
        model_slug=identity or None,
    )


def reject_unlisted_image(upstream: Mapping[str, Any], payload: Mapping[str, Any] | None) -> None:
    """Reject image input before Vision Proxy can describe or drop the part."""
    if not isinstance(payload, Mapping) or not _contains_image(payload):
        return
    identity = canonical_model_id(str(upstream.get("model_id") or upstream.get("upstream_model") or ""))
    selected = next((model for model in listed_models(read_status()) if model["id"] == identity), None)
    if selected is None:
        return
    _raise_if_image_unlisted(selected, payload, identity)


def _process_generation(status: Mapping[str, Any]) -> str | None:
    process = status.get("process")
    if not isinstance(process, Mapping) or process.get("running") is not True:
        return None
    pid = process.get("pid")
    port = process.get("port")
    if isinstance(pid, bool) or isinstance(port, bool):
        return None
    if not isinstance(pid, int) or not isinstance(port, int):
        return None
    return f"{pid}:{port}"


def sync_process_generation(status: Mapping[str, Any]) -> None:
    """Revoke tool permission when the supervised process identity changes."""
    global _PROCESS_GENERATION
    generation = _process_generation(status)
    with _TOOL_LOCK:
        if generation == _PROCESS_GENERATION:
            return
        _PROCESS_GENERATION = generation
        for permit in _CALLS.values():
            if permit.state in {"open", "forwarding", "recorded"}:
                permit.state = "revoked"
                permit.response_body = None
        _EPOCHS.clear()
        _FORWARDED.clear()
        _SUPPRESSED.clear()
    import chatgpt_web_client_session
    import chatgpt_web_collab

    chatgpt_web_client_session.revoke_all_tool_permissions()
    chatgpt_web_collab.revoke_all_permissions()


def _revoke_thread_locked(thread_id: str) -> None:
    for permit in _CALLS.values():
        if permit.thread_id != thread_id:
            continue
        if permit.state in {"open", "forwarding", "recorded"}:
            permit.state = "revoked"
            permit.response_body = None


def note_turn_epoch(thread_id: str, model_id: str, effort: str | None) -> bool:
    """Start a new epoch when model or effort changes. Return True if permits were revoked."""
    if not thread_id or not model_id:
        return False
    changed = False
    with _TOOL_LOCK:
        previous = _EPOCHS.get(thread_id)
        if previous is None:
            _EPOCHS[thread_id] = (model_id, effort)
        else:
            resolved = effort if effort is not None else previous[1]
            current = (model_id, resolved)
            if current != previous:
                _EPOCHS[thread_id] = current
                _revoke_thread_locked(thread_id)
                changed = True
    if not changed:
        return False
    import chatgpt_web_client_session
    import chatgpt_web_collab

    chatgpt_web_client_session.revoke_thread(thread_id)
    chatgpt_web_collab.revoke_thread(thread_id)
    return True


def _payload_request_kind(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    metadata = payload.get("client_metadata")
    raw = metadata.get("x-codex-turn-metadata") if isinstance(metadata, Mapping) else None
    parsed: Any = None
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    elif isinstance(raw, Mapping):
        parsed = raw
    if isinstance(parsed, Mapping):
        kind = parsed.get("request_kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip().lower()
    kind = payload.get("request_kind")
    if isinstance(kind, str) and kind.strip():
        return kind.strip().lower()
    return None


def _is_compaction_request(payload: Mapping[str, Any] | None, event_context: Mapping[str, Any] | None) -> bool:
    if isinstance(event_context, Mapping):
        kind = event_context.get("request_kind")
        if isinstance(kind, str) and kind.strip().lower() in _COMPACT_KINDS:
            return True
        path = event_context.get("path")
        if isinstance(path, str) and path.split("?", 1)[0].rstrip("/").endswith("/responses/compact"):
            return True
    if _payload_request_kind(payload) in _COMPACT_KINDS:
        return True
    if not isinstance(payload, Mapping):
        return False
    from gateway_stream_semantics import is_compact_summary_payload

    return is_compact_summary_payload(payload, "responses") or is_compact_summary_payload(payload, "chat_completions")


def _message_text(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in {"input_text", "text", "output_text"} and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def _user_message_keys(item: Mapping[str, Any]) -> set[str]:
    item_type = item.get("type")
    role = item.get("role")
    if item_type not in {None, "message"}:
        return set()
    if item_type == "message" and role not in {None, "user"}:
        return set()
    if item_type is None and role != "user":
        return set()
    keys: set[str] = set()
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        keys.add("id:" + item_id)
    text = _message_text(item)
    if text:
        keys.add("hash:" + hashlib.sha256(text.encode("utf-8")).hexdigest())
    return keys


def _payload_user_items(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    items = list(_input_items(payload))
    messages = payload.get("messages")
    if isinstance(messages, list):
        items.extend(message for message in messages if isinstance(message, Mapping))
    return items


def clear_message_filter() -> None:
    _BIND_FILTER.suppress = frozenset()


def _arm_message_filter(suppress: frozenset[str]) -> None:
    _BIND_FILTER.suppress = suppress


def _take_message_filter() -> frozenset[str]:
    suppress = getattr(_BIND_FILTER, "suppress", frozenset())
    if not isinstance(suppress, frozenset):
        suppress = frozenset()
    _BIND_FILTER.suppress = frozenset()
    return suppress


def plan_message_filter(
    thread_id: str,
    payload: Mapping[str, Any] | None,
    *,
    compaction: bool,
) -> tuple[frozenset[str], frozenset[str]]:
    """Arm removal of user messages that must not be sent on this turn."""
    with _TOOL_LOCK:
        source = _FORWARDED if compaction else _SUPPRESSED
        suppress = frozenset(source.get(thread_id, ()))
    forward: set[str] = set()
    for item in _payload_user_items(payload):
        keys = _user_message_keys(item)
        if not keys or keys & suppress:
            continue
        forward.update(keys)
    _arm_message_filter(suppress)
    return suppress, frozenset(forward)


def _drop_suppressed_items(items: list[Any], suppress: frozenset[str]) -> list[Any]:
    if not suppress:
        return items
    kept: list[Any] = []
    for item in items:
        if isinstance(item, Mapping) and _user_message_keys(item) & suppress:
            continue
        kept.append(item)
    return kept


def apply_message_filter(payload: dict[str, Any]) -> None:
    suppress = _take_message_filter()
    items = payload.get("input")
    if suppress and isinstance(items, list):
        payload["input"] = _drop_suppressed_items(items, suppress)


def remember_forwarded_messages(thread_id: str, keys: frozenset[str] | set[str]) -> None:
    if not thread_id or not keys:
        return
    with _TOOL_LOCK:
        _FORWARDED.setdefault(thread_id, set()).update(keys)


def complete_compaction(thread_id: str, keys: frozenset[str] | set[str]) -> None:
    if not thread_id or not keys:
        return
    with _TOOL_LOCK:
        _SUPPRESSED.setdefault(thread_id, set()).update(keys)


def refuse_submitted_retry(event_context: Mapping[str, Any] | None) -> None:
    """Do not open another POST for a turn that was already submitted."""
    inflight = _inflight_from(event_context)
    submitted = inflight is not None and inflight.submitted
    if not submitted:
        import chatgpt_web_client_session

        submitted = chatgpt_web_client_session.turn_was_submitted(event_context)
    if not submitted:
        return
    _raise_for_status(read_status(), "")
    raise identity_failure(
        "ChatGPT Web request was already submitted",
        reason=REASON_ALREADY_SUBMITTED,
        provider_id=PROVIDER_ID,
        model_slug=None,
    )


chatgpt_web_collab.install(globals())
