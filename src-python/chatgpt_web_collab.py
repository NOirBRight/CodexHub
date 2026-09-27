"""ChatGPT Web handling for plaintext Codex collaboration V2 calls.

The Gateway does not create agents, schedule them, or change the caller's
global V1/V2 choice. It forwards the six ``collaboration`` operations and
rejects ciphertext before ``POST /v1/responses``. A waiting parent must not
park a child turn: admission either accepts the new submission or rejects it
immediately.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Mapping

from collaboration_runtime_contract import V2_NAMESPACE, V2_TOOLS
from gateway_errors import identity_failure

PROVIDER_ID = "chatgpt-web"
MAX_INFLIGHT_WEB_TURNS = 5
V2_OPS = frozenset(V2_TOOLS)

REASON_ENCRYPTED = "chatgpt_web_collaboration_encrypted"
REASON_CAPACITY = "chatgpt_web_collaboration_capacity"
REASON_REVOKED = "chatgpt_web_collaboration_revoked"

ENCRYPTED_MESSAGE = "ChatGPT Web collaboration input is encrypted and unsupported"
CAPACITY_MESSAGE = "ChatGPT Web collaboration submission exceeds the bounded capacity"
REVOKED_MESSAGE = "ChatGPT Web collaboration call was revoked"

_PRESERVE_CALL_FIELDS = (
    "namespace",
    "name",
    "call_id",
    "id",
    "arguments",
    "encrypted_function_args",
)
_ADMISSION_KEY = "chatgpt_web_collab_admission"

_LOCK = threading.Lock()
_SLOTS: set[int] = set()
_NEXT_SLOT = 0
_PERMITS: dict[str, "_Permit"] = {}


@dataclass
class _Permit:
    thread_id: str
    turn_id: str
    call_id: str
    name: str
    state: str = "open"


@dataclass
class _Admission:
    slot: int | None = None
    consumed: list[str] = field(default_factory=list)
    submitted: bool = False
    replayed: bool = False
    finished: bool = False


def install(namespace: dict[str, Any]) -> None:
    """Wrap the web route once so collaboration checks run before a POST."""
    if namespace.get("_chatgpt_web_collab_installed"):
        return
    namespace["_chatgpt_web_collab_installed"] = True
    original_prepare = namespace["prepare_responses_exchange"]
    original_guard = namespace["submission_guard"]
    original_observe = namespace["observe_upstream_event"]
    original_note = namespace["note_upstream_submitted"]
    original_bind = namespace["bind_responses_body"]

    def prepare_responses_exchange(upstream, payload, event_context, admission):
        collab_admission = begin_exchange(upstream, payload)
        if isinstance(event_context, dict):
            event_context[_ADMISSION_KEY] = collab_admission
        try:
            replay = original_prepare(upstream, payload, event_context, admission)
        except BaseException:
            abort_exchange(collab_admission)
            if isinstance(event_context, dict):
                event_context.pop(_ADMISSION_KEY, None)
            raise
        collab_admission.replayed = replay is not None
        if replay is not None:
            collab_admission.submitted = True
        return replay

    @contextmanager
    def submission_guard(event_context):
        try:
            with original_guard(event_context):
                yield
        finally:
            finish_exchange(event_context)

    def observe_upstream_event(event, event_context):
        original_observe(event, event_context)
        note_stream_event(event, event_context)

    def note_upstream_submitted(event_context):
        original_note(event_context)
        collab_admission = _admission_from(event_context)
        if collab_admission is not None:
            collab_admission.submitted = True

    def bind_responses_body(body: bytes) -> bytes:
        return preserve_collaboration_wire(body, original_bind(body))

    namespace["prepare_responses_exchange"] = prepare_responses_exchange
    namespace["submission_guard"] = submission_guard
    namespace["observe_upstream_event"] = observe_upstream_event
    namespace["note_upstream_submitted"] = note_upstream_submitted
    namespace["bind_responses_body"] = bind_responses_body


def revoke_thread(thread_id: str) -> None:
    with _LOCK:
        _revoke_thread_locked(thread_id, keep=set())


def revoke_all_permissions() -> None:
    with _LOCK:
        for permit in _PERMITS.values():
            if permit.state in {"open", "consumed"}:
                permit.state = "revoked"


def reset_process_state() -> None:
    """Drop in-process turn slots and call permission. Tests only."""
    global _NEXT_SLOT
    with _LOCK:
        _SLOTS.clear()
        _PERMITS.clear()
        _NEXT_SLOT = 0


def begin_exchange(upstream: Mapping[str, Any], payload: Mapping[str, Any] | None) -> _Admission:
    admission = _Admission()
    if not isinstance(payload, Mapping) or not _is_collaboration_submission(payload):
        admission.slot = _reserve_slot()
        return admission
    calls = _collaboration_calls(payload)
    if any(_is_encrypted(item) for item in calls):
        _fail(upstream, ENCRYPTED_MESSAGE, REASON_ENCRYPTED)
    saturated = _capacity_saturated()
    thread_id, turn_id = _caller(payload) or ("", "")
    with _LOCK:
        if saturated or len(_SLOTS) >= MAX_INFLIGHT_WEB_TURNS:
            _fail(upstream, CAPACITY_MESSAGE, REASON_CAPACITY)
        admission.consumed = _claim_results_locked(calls, payload, thread_id, turn_id, upstream)
        admission.slot = _reserve_locked()
    return admission


def abort_exchange(admission: _Admission) -> None:
    with _LOCK:
        _release_locked(admission, rollback=not admission.submitted and not admission.replayed)


def finish_exchange(event_context: Mapping[str, Any] | None) -> None:
    admission = _admission_from(event_context)
    inflight = _inflight_from(event_context)
    with _LOCK:
        if inflight is not None and getattr(inflight, "aborted", False):
            _revoke_turn_locked(inflight.thread_id, inflight.turn_id)
        if admission is not None:
            rollback = (
                not admission.submitted
                and not admission.replayed
                and not (inflight is not None and getattr(inflight, "aborted", False))
            )
            _release_locked(admission, rollback=rollback)
    if isinstance(event_context, dict):
        event_context.pop(_ADMISSION_KEY, None)


def note_stream_event(event: Mapping[str, Any], event_context: Mapping[str, Any] | None) -> None:
    inflight = _inflight_from(event_context)
    if inflight is None or not isinstance(event, Mapping):
        return
    found = _collaboration_calls_in_event(event)
    if not found:
        return
    with _LOCK:
        for call_id, name in found:
            permit = _PERMITS.get(call_id)
            if permit is None:
                _PERMITS[call_id] = _Permit(inflight.thread_id, inflight.turn_id, call_id, name)
            elif permit.state == "open" and not permit.thread_id:
                permit.thread_id = inflight.thread_id
                permit.turn_id = inflight.turn_id
            if name == "interrupt_agent":
                _revoke_thread_locked(inflight.thread_id, keep={call_id})


def preserve_collaboration_wire(original: bytes, bound: bytes) -> bytes:
    """Put namespace, plaintext marker, call identity, and model back if dropped."""
    try:
        source = json.loads(original.decode("utf-8-sig"))
        payload = json.loads(bound.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return bound
    if not isinstance(source, dict) or not isinstance(payload, dict):
        return bound
    changed = False
    if "model" in source and payload.get("model") != source.get("model"):
        payload["model"] = source.get("model")
        changed = True
    source_items = source.get("input")
    bound_items = payload.get("input")
    if isinstance(source_items, list) and isinstance(bound_items, list):
        for src, dst in zip(source_items, bound_items):
            if not isinstance(src, dict) or not isinstance(dst, dict):
                continue
            if src.get("type") == "function_call" and src.get("namespace") == V2_NAMESPACE:
                for key in _PRESERVE_CALL_FIELDS:
                    if key in src and dst.get(key) != src.get(key):
                        dst[key] = src.get(key)
                        changed = True
            elif src.get("type") == "function_call_output":
                for key in ("call_id", "output"):
                    if key in src and dst.get(key) != src.get(key):
                        dst[key] = src.get(key)
                        changed = True
    if not changed:
        return bound
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_collaboration_submission(payload: Mapping[str, Any]) -> bool:
    if _declares_collaboration(payload.get("tools")):
        return True
    if _collaboration_calls(payload):
        return True
    return any(item.get("call_id") in _PERMITS for item in _output_items(payload))


def _declares_collaboration(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    return any(_tool_is_collaboration(tool) for tool in tools)


def _tool_is_collaboration(tool: Any) -> bool:
    if not isinstance(tool, Mapping):
        return False
    if tool.get("namespace") == V2_NAMESPACE:
        return True
    if tool.get("type") == "namespace" and tool.get("name") == V2_NAMESPACE:
        return True
    nested = tool.get("tools")
    return isinstance(nested, list) and any(_tool_is_collaboration(child) for child in nested)


def _collaboration_calls(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in _input_items(payload)
        if item.get("type") == "function_call" and item.get("namespace") == V2_NAMESPACE and item.get("name") in V2_OPS
    ]


def _output_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in _input_items(payload) if item.get("type") == "function_call_output"]


def _input_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = payload.get("input")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def _is_encrypted(item: Mapping[str, Any]) -> bool:
    if "encrypted_function_args" not in item:
        return False
    return item.get("encrypted_function_args") != []


def _caller(payload: Mapping[str, Any]) -> tuple[str, str] | None:
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
    return thread_id, turn_id


def _capacity_saturated() -> bool:
    import chatgpt_web_route

    status = chatgpt_web_route.read_status()
    return isinstance(status, Mapping) and status.get("capacity") == "saturated"


def _claim_results_locked(
    calls: list[Mapping[str, Any]],
    payload: Mapping[str, Any],
    thread_id: str,
    turn_id: str,
    upstream: Mapping[str, Any],
) -> list[str]:
    for item in calls:
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        permit = _PERMITS.get(call_id)
        if permit is None:
            _PERMITS[call_id] = _Permit(thread_id, turn_id, call_id, str(item.get("name") or ""))
        elif permit.state == "revoked":
            _fail(upstream, REVOKED_MESSAGE, REASON_REVOKED)
    if any(item.get("name") == "interrupt_agent" for item in calls):
        interrupt_ids = {
            item.get("call_id")
            for item in calls
            if item.get("name") == "interrupt_agent" and isinstance(item.get("call_id"), str)
        }
        _revoke_thread_locked(thread_id, keep={call_id for call_id in interrupt_ids if isinstance(call_id, str)})
    consumed: list[str] = []
    call_ids = {item.get("call_id") for item in calls if isinstance(item.get("call_id"), str)}
    for item in _output_items(payload):
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if call_id not in call_ids and call_id not in _PERMITS:
            continue
        permit = _PERMITS.get(call_id)
        if permit is None or permit.name not in V2_OPS:
            continue
        if permit.state == "revoked":
            _fail(upstream, REVOKED_MESSAGE, REASON_REVOKED)
        if permit.state == "consumed":
            permit.state = "revoked"
            _fail(upstream, REVOKED_MESSAGE, REASON_REVOKED)
        if permit.state == "open":
            permit.state = "consumed"
            consumed.append(call_id)
    return consumed


def _collaboration_calls_in_event(event: Mapping[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []

    def remember(item: Any) -> None:
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            return
        if item.get("namespace") != V2_NAMESPACE or item.get("name") not in V2_OPS:
            return
        call_id = item.get("call_id")
        name = item.get("name")
        if isinstance(call_id, str) and call_id and isinstance(name, str):
            found.append((call_id, name))

    event_type = event.get("type")
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        remember(event.get("item"))
    elif event_type == "response.completed":
        response = event.get("response")
        output = response.get("output") if isinstance(response, Mapping) else None
        if isinstance(output, list):
            for item in output:
                remember(item)
    return found


def _revoke_thread_locked(thread_id: str, *, keep: set[str]) -> None:
    if not thread_id:
        return
    for permit in _PERMITS.values():
        if permit.thread_id != thread_id or permit.call_id in keep:
            continue
        if permit.state in {"open", "consumed"}:
            permit.state = "revoked"


def _revoke_turn_locked(thread_id: str, turn_id: str) -> None:
    for permit in _PERMITS.values():
        if permit.thread_id != thread_id or permit.turn_id != turn_id:
            continue
        if permit.state in {"open", "consumed"}:
            permit.state = "revoked"


def _release_locked(admission: _Admission, *, rollback: bool) -> None:
    if admission.finished:
        return
    admission.finished = True
    if admission.slot is not None:
        _SLOTS.discard(admission.slot)
        admission.slot = None
    if not rollback:
        return
    for call_id in admission.consumed:
        permit = _PERMITS.get(call_id)
        if permit is not None and permit.state == "consumed":
            permit.state = "open"
    admission.consumed = []


def _reserve_slot() -> int:
    with _LOCK:
        return _reserve_locked()


def _reserve_locked() -> int:
    global _NEXT_SLOT
    _NEXT_SLOT += 1
    _SLOTS.add(_NEXT_SLOT)
    return _NEXT_SLOT


def _admission_from(event_context: Mapping[str, Any] | None) -> _Admission | None:
    if not isinstance(event_context, Mapping):
        return None
    admission = event_context.get(_ADMISSION_KEY)
    return admission if isinstance(admission, _Admission) else None


def _inflight_from(event_context: Mapping[str, Any] | None) -> Any:
    if not isinstance(event_context, Mapping):
        return None
    inflight = event_context.get("chatgpt_web_inflight")
    if inflight is None:
        return None
    if hasattr(inflight, "thread_id") and hasattr(inflight, "turn_id"):
        return inflight
    return None


def _fail(upstream: Mapping[str, Any], message: str, reason: str) -> None:
    from catalog import canonical_model_id

    raw = ""
    if isinstance(upstream, Mapping):
        value = upstream.get("model_id") or upstream.get("upstream_model") or ""
        raw = value if isinstance(value, str) else ""
    slug = canonical_model_id(raw) if raw else None
    raise identity_failure(
        message,
        reason=reason,
        provider_id=PROVIDER_ID,
        model_slug=slug,
    )
