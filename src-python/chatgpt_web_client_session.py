"""Non-Codex session binding for ChatGPT Web.

Chat Completions and Anthropic Messages share this table. A web turn is bound
only from an explicit client session id. The same id keeps one thread. Tool
results stay on the call id that session already owns.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from catalog import canonical_model_id
from gateway_errors import UpstreamProtocolTranslationError, identity_failure
from protocol_translation import UnsupportedProtocolTranslationError

import chatgpt_web_route

PROVIDER_ID = chatgpt_web_route.PROVIDER_ID
REASON_SESSION_REQUIRED = "chatgpt_web_client_session_required"
REASON_TOOL_CALL_REJECTED = "chatgpt_web_client_tool_call_rejected"
_HEADER_COPIES = ("X-Session-Id", "x-session-id")
_BODY_FIELDS = ("sessionID", "session_id")
_SESSION_FIELDS = _HEADER_COPIES + _BODY_FIELDS
_BINDING_KEY = "chatgpt_web_client_binding"
_INFLIGHT_KEY = "chatgpt_web_client_inflight"


def client_session_id(
    payload: Mapping[str, Any] | None,
    event_context: Mapping[str, Any] | None,
) -> str | None:
    """Return the explicit session id, or None when the caller did not send one.

    Header copies win over an OpenCode body field. Prompt text, the HTTP
    connection, and an unscoped call id are not session identities.
    """
    for source in (event_context, payload):
        if not isinstance(source, Mapping):
            continue
        for key in _HEADER_COPIES:
            found = _text(source.get(key))
            if found:
                return found
    if isinstance(event_context, Mapping):
        found = _text(event_context.get("session_id"))
        if found:
            return found
    if isinstance(payload, Mapping):
        for key in _BODY_FIELDS:
            found = _text(payload.get(key))
            if found:
                return found
    return None


def thread_id_for(session_id: str) -> str:
    digest = hashlib.sha256(
        b"codexhub:chatgpt-web-client-session:v1\0" + session_id.encode("utf-8")
    ).hexdigest()
    return "thread_" + digest[:32]


def prepare_responses_exchange(
    upstream: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
    event_context: dict[str, Any] | None,
    admission: Any | None,
) -> bytes | None:
    """Admit one non-Codex turn. Return recorded SSE when a tool result is replayed."""
    slug = _slug(upstream)
    session_id = client_session_id(payload, event_context)
    if session_id is None:
        _session_failure(slug)
    chatgpt_web_route.ensure_exchange_allowed(upstream, _readiness_payload(payload))
    thread_id = thread_id_for(session_id)
    call_ids = _tool_result_ids(payload)
    replay_body: bytes | None = None
    turn_id: str | None = None
    if call_ids is None:
        _tool_failure("ChatGPT Web tool call is unknown", slug)
    if call_ids:
        replay_body, turn_id = _claim_tool_results(session_id, call_ids, slug)
    if turn_id is None:
        turn_id = "turn_" + uuid.uuid4().hex
    if isinstance(event_context, dict):
        event_context[_BINDING_KEY] = {
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
        }
    if replay_body is not None:
        return replay_body
    if not isinstance(event_context, dict):
        return None
    inflight = _Inflight(
        session_id=session_id,
        thread_id=thread_id,
        turn_id=turn_id,
        phase="continue" if call_ids else "issue",
        call_ids=call_ids,
    )
    event_context[_INFLIGHT_KEY] = inflight
    if admission is not None:
        add_listener = getattr(admission, "add_cancel_listener", None)
        if callable(add_listener):
            add_listener(lambda: _abort_inflight(inflight))
    return None


def prepare_attempt_body(request: Any, attempt: Any) -> tuple[Any, bytes]:
    """Convert the caller body to Responses and stamp this session's thread."""
    payload = request.inbound_payload if isinstance(request.inbound_payload, Mapping) else None
    event_context = request.event_context if isinstance(request.event_context, Mapping) else None
    session_id = client_session_id(payload, event_context)
    if session_id is None:
        _session_failure(_slug(request.upstream))
    try:
        prepared = attempt.prepare_body(_without_session_fields(request.prepared_body))
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc
    try:
        responses = json.loads(prepared.upstream_body.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return prepared, prepared.upstream_body
    if not isinstance(responses, dict):
        return prepared, prepared.upstream_body
    binding = _binding(event_context, session_id)
    responses["prompt_cache_key"] = binding["thread_id"]
    responses["client_metadata"] = {
        "x-codex-turn-metadata": json.dumps(
            {
                "thread_id": binding["thread_id"],
                "turn_id": binding["turn_id"],
                "request_kind": "turn",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }
    _stamp_user_messages(responses, session_id, binding["turn_id"])
    body = json.dumps(responses, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prepared = replace(prepared, upstream_body=body)
    return prepared, body


def note_upstream_submitted(event_context: Mapping[str, Any] | None) -> None:
    inflight = _inflight_from(event_context)
    if inflight is None:
        return
    with _LOCK:
        inflight.submitted = True


@contextmanager
def submission_guard(event_context: Mapping[str, Any] | None) -> Iterator[None]:
    try:
        yield
    finally:
        _settle_submission(event_context)


def observe_upstream_event(event: Mapping[str, Any], event_context: Mapping[str, Any] | None) -> None:
    """Record function-call ids emitted for this client session."""
    inflight = _inflight_from(event_context)
    if inflight is None or not isinstance(event, Mapping):
        return
    try:
        copied = json.loads(json.dumps(event))
    except (TypeError, ValueError):
        return
    if not isinstance(copied, dict):
        return
    with _LOCK:
        if inflight.phase == "issue":
            for call_id in _function_call_ids(copied):
                if call_id not in _CALLS:
                    _CALLS[call_id] = _Permit(
                        session_id=inflight.session_id,
                        thread_id=inflight.thread_id,
                        turn_id=inflight.turn_id,
                        call_id=call_id,
                    )
                inflight.observed.add(call_id)
        inflight.events.append(copied)
        if copied.get("type") != "response.completed":
            return
        inflight.terminal_seen = True
        if inflight.phase != "continue":
            return
        body = _encode_sse(inflight.events)
        for call_id in inflight.call_ids:
            permit = _CALLS.get(call_id)
            if (
                permit is not None
                and permit.state == "forwarding"
                and permit.session_id == inflight.session_id
            ):
                permit.state = "recorded"
                permit.response_body = body


@dataclass
class _Permit:
    session_id: str
    thread_id: str
    turn_id: str
    call_id: str
    state: str = "open"
    response_body: bytes | None = None


@dataclass
class _Inflight:
    session_id: str
    thread_id: str
    turn_id: str
    phase: str
    call_ids: tuple[str, ...] = ()
    observed: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    submitted: bool = False
    terminal_seen: bool = False
    aborted: bool = False
    settled: bool = False


_LOCK = threading.Lock()
_CALLS: dict[str, _Permit] = {}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _slug(upstream: Mapping[str, Any] | None) -> str:
    if not isinstance(upstream, Mapping):
        return ""
    raw = upstream.get("model_id") or upstream.get("upstream_model") or ""
    if not isinstance(raw, str) or not raw:
        return ""
    return canonical_model_id(raw)


def _session_failure(slug: str) -> None:
    raise identity_failure(
        "ChatGPT Web client session id is required",
        reason=REASON_SESSION_REQUIRED,
        provider_id=PROVIDER_ID,
        model_slug=slug or None,
    )


def _tool_failure(message: str, slug: str) -> None:
    raise identity_failure(
        message,
        reason=REASON_TOOL_CALL_REJECTED,
        provider_id=PROVIDER_ID,
        model_slug=slug or None,
    )


def _readiness_payload(payload: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Drop client-executed tools so the Codex tunnel gate is not applied."""
    if not isinstance(payload, Mapping):
        return payload
    return {key: value for key, value in payload.items() if key not in {"tools", "tool_choice"}}


def _tool_result_ids(payload: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    if not isinstance(payload, Mapping):
        return ()
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ()
    call_ids: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                return None
            if call_id not in call_ids:
                call_ids.append(call_id)
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id.strip():
                return None
            if call_id not in call_ids:
                call_ids.append(call_id)
    return tuple(call_ids)


def _without_session_fields(body: bytes) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    if not any(key in payload for key in _SESSION_FIELDS):
        return body
    stripped = {key: value for key, value in payload.items() if key not in _SESSION_FIELDS}
    return json.dumps(stripped, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _binding(event_context: Mapping[str, Any] | None, session_id: str) -> dict[str, str]:
    if isinstance(event_context, Mapping):
        stored = event_context.get(_BINDING_KEY)
        if (
            isinstance(stored, Mapping)
            and stored.get("session_id") == session_id
            and isinstance(stored.get("thread_id"), str)
            and stored.get("thread_id")
            and isinstance(stored.get("turn_id"), str)
            and stored.get("turn_id")
        ):
            return {
                "session_id": session_id,
                "thread_id": stored["thread_id"],
                "turn_id": stored["turn_id"],
            }
    return {
        "session_id": session_id,
        "thread_id": thread_id_for(session_id),
        "turn_id": "turn_" + uuid.uuid4().hex,
    }


def _stamp_user_messages(payload: dict[str, Any], session_id: str, turn_id: str) -> None:
    items = payload.get("input")
    if not isinstance(items, list):
        return
    prefix = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "user":
            continue
        item.setdefault("id", f"msg_{prefix}_{index}")
        passthrough = item.get("internal_chat_message_metadata_passthrough")
        if isinstance(passthrough, dict):
            passthrough.setdefault("turn_id", turn_id)
        else:
            item["internal_chat_message_metadata_passthrough"] = {"turn_id": turn_id}


def _claim_tool_results(
    session_id: str,
    call_ids: tuple[str, ...],
    slug: str,
) -> tuple[bytes | None, str]:
    with _LOCK:
        permits: list[_Permit] = []
        for call_id in call_ids:
            permit = _CALLS.get(call_id)
            if permit is None:
                _tool_failure("ChatGPT Web tool call is unknown", slug)
            assert permit is not None
            if permit.session_id != session_id:
                _tool_failure("ChatGPT Web tool call belongs to another client session", slug)
            if permit.state == "revoked":
                _tool_failure("ChatGPT Web tool call expired", slug)
            if permit.state == "forwarding":
                _tool_failure("ChatGPT Web tool call is already in flight", slug)
            if permit.state not in {"open", "recorded"}:
                _tool_failure("ChatGPT Web tool call expired", slug)
            permits.append(permit)
        turn_id = permits[0].turn_id
        if any(permit.turn_id != turn_id for permit in permits):
            _tool_failure("ChatGPT Web tool call is not active on this session", slug)
        if all(permit.state == "recorded" and permit.response_body for permit in permits):
            return permits[-1].response_body, turn_id
        if any(permit.state != "open" for permit in permits):
            _tool_failure("ChatGPT Web tool call expired", slug)
        for permit in permits:
            permit.state = "forwarding"
            permit.response_body = None
        return None, turn_id


def _abort_inflight(inflight: _Inflight) -> None:
    with _LOCK:
        if inflight.terminal_seen or inflight.aborted:
            return
        inflight.aborted = True
        _revoke_locked(inflight)


def _settle_submission(event_context: Mapping[str, Any] | None) -> None:
    inflight = _inflight_from(event_context)
    if inflight is None:
        return
    with _LOCK:
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
                    and permit.session_id == inflight.session_id
                ):
                    permit.state = "open"
            return
        _revoke_locked(inflight)


def _revoke_locked(inflight: _Inflight) -> None:
    call_ids = inflight.call_ids if inflight.phase == "continue" else tuple(inflight.observed)
    for call_id in call_ids:
        permit = _CALLS.get(call_id)
        if permit is None or permit.session_id != inflight.session_id:
            continue
        if permit.state in {"open", "forwarding"}:
            permit.state = "revoked"
            permit.response_body = None


def _inflight_from(event_context: Mapping[str, Any] | None) -> _Inflight | None:
    if not isinstance(event_context, Mapping):
        return None
    inflight = event_context.get(_INFLIGHT_KEY)
    return inflight if isinstance(inflight, _Inflight) else None


def _function_call_ids(event: Mapping[str, Any]) -> list[str]:
    found: list[str] = []

    def remember(item: Any) -> None:
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            return
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id and call_id not in found:
            found.append(call_id)

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
        if isinstance(call_id, str) and call_id and call_id not in found:
            found.append(call_id)
    return found


def _encode_sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(
        b"data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
        for event in events
    )
