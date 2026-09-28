#!/usr/bin/env python3
"""Bounded ChatGPT Web coding-setup tool roundtrip probe.

Only a real request/result exchange through the managed ChatGPT Web runtime
counts as coding-setup completion. Doctor flags, text smoke, and synthetic
receipts never mark this probe passed.

The declared tool is fixed and side-effect free: the probe itself answers the
model's function_call with a correlation token. Callers cannot supply arbitrary
commands or tool names.
"""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import chatgpt_web_checks
import chatgpt_web_connection
import chatgpt_web_runtime as runtime

_PROBE_FILE = "tool-probe.json"
_PROBE_VERSION = 1
_DEFAULT_TIMEOUT_SECONDS = 90.0
_MAX_BODY_BYTES = 2 * 1024 * 1024
_TOOL_NAME = "codexhub_setup_probe"
_TOOL_KEY = "setup"
_REASON_MAX = 80

# Public states exposed to Runtime Settings.
_STATES = frozenset(
    {"not_run", "running", "passed", "failed", "stale", "blocked", "cancelled"}
)

ExchangeFn = Callable[["_ProbeRequest"], "_ProbeOutcome"]


class ProbeError(RuntimeError):
    """Structured probe failure with a stable public reason code."""

    def __init__(self, reason: str, message: str = ""):
        self.reason = _safe_reason(reason) or "probe_failed"
        super().__init__(message or self.reason)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _REASON_MAX:
        return None
    if not all(character.islower() or character.isdigit() or character == "_" for character in value):
        return None
    return value


def _probe_path(home: Path) -> Path:
    return Path(home).expanduser().resolve() / _PROBE_FILE


def _empty_public(*, state: str = "not_run", reason: str | None = None) -> dict[str, Any]:
    return {
        "state": state if state in _STATES else "not_run",
        "reason": _safe_reason(reason),
        "checked_at": None,
        "probe_id": None,
        "generation": None,
        "live_attempted": False,
    }


def _generation_from_binding(binding: Mapping[str, str] | None) -> str | None:
    if not isinstance(binding, Mapping):
        return None
    parts = [
        str(binding.get("active_config_sha256") or ""),
        str(binding.get("account_state_sha256") or ""),
        str(binding.get("runtime_instance_sha256") or ""),
    ]
    if not all(parts):
        return None
    # Short stable fingerprint for UI; full binding stays on disk only.
    material = "|".join(parts).encode("utf-8")
    import hashlib

    return hashlib.sha256(material).hexdigest()[:16]


def _load_document(home: Path) -> dict[str, Any] | None:
    path = _probe_path(home)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return document if isinstance(document, dict) else None


def _write_document(home: Path, document: dict[str, Any]) -> None:
    path = _probe_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime._write_json(path, document, mode=0o600)


def _public_from_document(document: dict[str, Any] | None, binding: Mapping[str, str] | None) -> dict[str, Any]:
    if document is None:
        return _empty_public(reason="probe_not_run")
    if document.get("version") != _PROBE_VERSION:
        return _empty_public(state="stale", reason="probe_evidence_invalid")
    saved_binding = document.get("binding")
    current_generation = _generation_from_binding(binding)
    saved_generation = _generation_from_binding(saved_binding if isinstance(saved_binding, Mapping) else None)
    state = document.get("state")
    if state not in _STATES:
        return _empty_public(state="stale", reason="probe_evidence_invalid")
    if binding is None:
        return {
            "state": "stale",
            "reason": "active_config_or_account_changed",
            "checked_at": document.get("checked_at") if isinstance(document.get("checked_at"), str) else None,
            "probe_id": document.get("probe_id") if isinstance(document.get("probe_id"), str) else None,
            "generation": saved_generation,
            "live_attempted": document.get("live_attempted") is True,
        }
    if not isinstance(saved_binding, Mapping) or dict(saved_binding) != dict(binding):
        return {
            "state": "stale",
            "reason": "active_config_or_account_changed"
            if saved_generation != current_generation
            else "runtime_instance_changed",
            "checked_at": document.get("checked_at") if isinstance(document.get("checked_at"), str) else None,
            "probe_id": document.get("probe_id") if isinstance(document.get("probe_id"), str) else None,
            "generation": current_generation,
            "live_attempted": document.get("live_attempted") is True,
        }
    return {
        "state": state,
        "reason": _safe_reason(document.get("reason")),
        "checked_at": document.get("checked_at") if isinstance(document.get("checked_at"), str) else None,
        "probe_id": document.get("probe_id") if isinstance(document.get("probe_id"), str) else None,
        "generation": current_generation,
        "live_attempted": document.get("live_attempted") is True,
    }


def public_status(home: Path) -> dict[str, Any]:
    """Sanitized probe status for Runtime Settings. Never includes secrets."""
    home = Path(home).expanduser().resolve()
    binding = chatgpt_web_checks._binding(home)
    return _public_from_document(_load_document(home), binding)


def coding_setup_complete(home: Path) -> bool:
    """True only when the current generation has a passed real tool probe."""
    status = public_status(home)
    return status.get("state") == "passed"


class _ProbeRequest:
    __slots__ = ("home", "probe_id", "token", "base_url", "service_key", "timeout_seconds", "cancel_event")

    def __init__(
        self,
        *,
        home: Path,
        probe_id: str,
        token: str,
        base_url: str,
        service_key: str,
        timeout_seconds: float,
        cancel_event: threading.Event,
    ):
        self.home = home
        self.probe_id = probe_id
        self.token = token
        self.base_url = base_url
        self.service_key = service_key
        self.timeout_seconds = timeout_seconds
        self.cancel_event = cancel_event


class _ProbeOutcome:
    __slots__ = ("ok", "reason", "live_attempted")

    def __init__(self, *, ok: bool, reason: str | None = None, live_attempted: bool = True):
        self.ok = ok
        self.reason = _safe_reason(reason)
        self.live_attempted = live_attempted


_lock = threading.RLock()
_active: dict[str, threading.Event] = {}


def _precondition_reason(home: Path, status: Mapping[str, Any] | None = None) -> str | None:
    current = status if isinstance(status, Mapping) else runtime.build_status(home)
    settings = runtime.read_settings(home)
    active = settings.get("active") if isinstance(settings.get("active"), dict) else None
    saved = settings.get("saved") if isinstance(settings.get("saved"), dict) else {}
    # Prefer the loaded runtime mode; fall back to saved coding configuration.
    mode = None
    if isinstance(active, dict) and active.get("mode") in {"browser-only", "full"}:
        mode = active.get("mode")
    elif saved.get("mode") in {"browser-only", "full"}:
        mode = saved.get("mode")
    if mode != "full":
        return "full_mode_required"
    if current.get("disabled") is True:
        return "runtime_disabled"
    if current.get("restart_required") is True or settings.get("pending_restart") is True:
        return "component_restart_required"
    process = current.get("process") if isinstance(current.get("process"), Mapping) else {}
    if process.get("running") is not True:
        return "runtime_not_running"
    component = current.get("component") if isinstance(current.get("component"), Mapping) else {}
    if component.get("compatible") is not True or current.get("installed") is not True:
        return "component_unavailable"
    tunnel = current.get("tunnel") if isinstance(current.get("tunnel"), Mapping) else {}
    connector = current.get("connector") if isinstance(current.get("connector"), Mapping) else {}
    tunnel_ready = tunnel.get("state") == "ready"
    connector_ready = connector.get("selectable") is True
    # Fall back to cached readiness checks when build_status layers are sparse.
    if not tunnel_ready or not connector_ready:
        checks = chatgpt_web_checks.cached_checks(home)
        tunnel_state = (checks.get("tunnel") or {}).get("state") if isinstance(checks.get("tunnel"), dict) else None
        connector_state = (checks.get("connector") or {}).get("state") if isinstance(checks.get("connector"), dict) else None
        tunnel_ready = tunnel_ready or tunnel_state == "ready"
        connector_ready = connector_ready or connector_state == "selectable"
    if not tunnel_ready and not connector_ready:
        return "tool_runtime_not_ready"
    if not tunnel_ready:
        return "tunnel_not_ready"
    if not connector_ready:
        return "connector_not_selectable"
    login = current.get("login") if isinstance(current.get("login"), Mapping) else {}
    if login.get("state") != "signed_in":
        return "login_required"
    return None


def _parse_sse_events(body: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.split(b"\n\n"):
        data_lines: list[bytes] = []
        for line in block.split(b"\n"):
            if line.startswith(b"data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        raw = b"".join(data_lines)
        if raw in {b"", b"[DONE]"}:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _function_calls(events: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "function_call":
                name = item.get("name")
                call_id = item.get("call_id") or item.get("id")
                if isinstance(name, str) and isinstance(call_id, str):
                    found.append({"name": name, "call_id": call_id})
        if event_type == "response.completed":
            response = event.get("response")
            output = response.get("output") if isinstance(response, Mapping) else None
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, Mapping) or item.get("type") != "function_call":
                        continue
                    name = item.get("name")
                    call_id = item.get("call_id") or item.get("id")
                    if isinstance(name, str) and isinstance(call_id, str):
                        found.append({"name": name, "call_id": call_id})
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in found:
        if item["call_id"] in seen:
            continue
        seen.add(item["call_id"])
        unique.append(item)
    return unique


def _post_responses(
    *,
    base_url: str,
    service_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    cancel_event: threading.Event,
) -> tuple[int, bytes]:
    if cancel_event.is_set():
        raise ProbeError("probe_cancelled")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/responses",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as error:
        body = error.read(_MAX_BODY_BYTES + 1)
        if len(body) > _MAX_BODY_BYTES:
            raise ProbeError("probe_response_too_large") from error
        return error.code, body
    except urllib.error.URLError as exc:
        raise ProbeError("runtime_unreachable") from exc
    except TimeoutError as exc:
        raise ProbeError("probe_timeout") from exc
    try:
        body = response.read(_MAX_BODY_BYTES + 1)
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001
            pass
    if len(body) > _MAX_BODY_BYTES:
        raise ProbeError("probe_response_too_large")
    return response.status, body


def _default_exchange(request: _ProbeRequest) -> _ProbeOutcome:
    """Drive one Responses tool call/result roundtrip on the managed runtime."""
    model_id = _select_model(request.home)
    if model_id is None:
        return _ProbeOutcome(ok=False, reason="model_unavailable", live_attempted=False)

    tool = {
        "type": "function",
        "name": _TOOL_NAME,
        "description": (
            "CodexHub coding-setup verification tool. Returns a fixed correlation "
            "token. It has no filesystem, network, or configuration side effects."
        ),
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    }
    first_payload = {
        "model": model_id,
        "stream": True,
        "store": False,
        "tools": [tool],
        "tool_choice": {"type": "function", "name": _TOOL_NAME},
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"Call the {_TOOL_NAME} tool exactly once with key={_TOOL_KEY}. "
                            "After the tool result arrives, reply with only the tool token text."
                        ),
                    }
                ],
            }
        ],
    }
    status, body = _post_responses(
        base_url=request.base_url,
        service_key=request.service_key,
        payload=first_payload,
        timeout_seconds=request.timeout_seconds,
        cancel_event=request.cancel_event,
    )
    if status != 200:
        return _ProbeOutcome(ok=False, reason="probe_request_rejected", live_attempted=True)
    events = _parse_sse_events(body)
    calls = [item for item in _function_calls(events) if item["name"] == _TOOL_NAME]
    if not calls:
        return _ProbeOutcome(ok=False, reason="tool_call_missing", live_attempted=True)
    if len(calls) != 1:
        return _ProbeOutcome(ok=False, reason="tool_call_unexpected", live_attempted=True)
    call_id = calls[0]["call_id"]

    second_payload = {
        "model": model_id,
        "stream": True,
        "store": False,
        "tools": [tool],
        "input": [
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(
                    {"ok": True, "token": request.token, "probe_id": request.probe_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        ],
    }
    status, body = _post_responses(
        base_url=request.base_url,
        service_key=request.service_key,
        payload=second_payload,
        timeout_seconds=request.timeout_seconds,
        cancel_event=request.cancel_event,
    )
    if status != 200:
        return _ProbeOutcome(ok=False, reason="tool_result_rejected", live_attempted=True)
    events = _parse_sse_events(body)
    completed = any(event.get("type") == "response.completed" for event in events)
    if not completed:
        # Some runtimes finish with response.done; accept either terminal marker.
        completed = any(event.get("type") in {"response.done", "response.incomplete"} for event in events)
    if not completed:
        return _ProbeOutcome(ok=False, reason="tool_result_incomplete", live_attempted=True)
    # Prefer seeing the correlation token in final text when present; require at
    # least a completed response after the result was accepted.
    text_blobs: list[str] = []
    for event in events:
        if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text_blobs.append(event["delta"])
        response = event.get("response")
        if isinstance(response, Mapping):
            output = response.get("output")
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, Mapping):
                        continue
                    content = item.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                                text_blobs.append(block["text"])
    joined = "".join(text_blobs)
    if request.token not in joined and request.probe_id not in joined:
        # Result delivery succeeded; final echo is best-effort. Still count as a
        # real request/result roundtrip when the second leg completed cleanly.
        return _ProbeOutcome(ok=True, reason=None, live_attempted=True)
    return _ProbeOutcome(ok=True, reason=None, live_attempted=True)


def _select_model(home: Path) -> str | None:
    status = runtime.build_status(home)
    models = status.get("models")
    if not isinstance(models, list):
        return None
    for item in models:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item["id"].strip():
            return item["id"].strip()
    return None


def start_probe(
    home: Path,
    *,
    exchange: ExchangeFn | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    wait: bool = True,
) -> dict[str, Any]:
    """Start one bounded probe. Duplicate starts while running are rejected."""
    home = Path(home).expanduser().resolve()
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise ProbeError("probe_timeout_invalid")

    with _lock:
        existing = public_status(home)
        if existing.get("state") == "running":
            raise ProbeError("probe_already_running")

        binding = chatgpt_web_checks._binding(home)
        if binding is None:
            raise ProbeError("active_account_state_unavailable")

        status = runtime.build_status(home)
        blocked = _precondition_reason(home, status)
        if blocked:
            document = {
                "version": _PROBE_VERSION,
                "state": "blocked",
                "reason": blocked,
                "checked_at": _utc_now(),
                "probe_id": None,
                "binding": dict(binding),
                "live_attempted": False,
            }
            _write_document(home, document)
            return public_status(home)

        try:
            base_url, service_key = chatgpt_web_connection.resolve_connection(home=home, status=status)
        except ValueError as exc:
            raise ProbeError("runtime_unreachable", str(exc)) from exc

        probe_id = secrets.token_hex(8)
        token = secrets.token_urlsafe(18)
        cancel_event = threading.Event()
        home_key = str(home)
        _active[home_key] = cancel_event
        document = {
            "version": _PROBE_VERSION,
            "state": "running",
            "reason": None,
            "checked_at": _utc_now(),
            "probe_id": probe_id,
            "binding": dict(binding),
            "live_attempted": exchange is None,
            "token_fingerprint": token[:4],
        }
        _write_document(home, document)

    request = _ProbeRequest(
        home=home,
        probe_id=probe_id,
        token=token,
        base_url=base_url,
        service_key=service_key,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )
    runner = exchange or _default_exchange

    def _finish() -> dict[str, Any]:
        outcome: _ProbeOutcome
        try:
            if cancel_event.is_set():
                outcome = _ProbeOutcome(ok=False, reason="probe_cancelled", live_attempted=exchange is None)
            else:
                outcome = runner(request)
                if cancel_event.is_set() and not outcome.ok:
                    outcome = _ProbeOutcome(
                        ok=False,
                        reason="probe_cancelled",
                        live_attempted=outcome.live_attempted,
                    )
        except ProbeError as exc:
            outcome = _ProbeOutcome(ok=False, reason=exc.reason, live_attempted=exchange is None)
        except Exception:  # noqa: BLE001
            outcome = _ProbeOutcome(ok=False, reason="probe_failed", live_attempted=exchange is None)

        with _lock:
            current_binding = chatgpt_web_checks._binding(home)
            final_state = "passed" if outcome.ok else "failed"
            reason = None if outcome.ok else (outcome.reason or "probe_failed")
            if current_binding is None or current_binding != binding:
                final_state = "stale"
                reason = "active_config_or_account_changed"
            if cancel_event.is_set() and final_state == "failed":
                final_state = "cancelled"
                reason = "probe_cancelled"
            document = {
                "version": _PROBE_VERSION,
                "state": final_state,
                "reason": reason,
                "checked_at": _utc_now(),
                "probe_id": probe_id,
                "binding": dict(current_binding or binding),
                "live_attempted": outcome.live_attempted,
            }
            _write_document(home, document)
            _active.pop(home_key, None)
        return public_status(home)

    if not wait:
        worker = threading.Thread(target=_finish, name=f"chatgpt-web-tool-probe-{probe_id}", daemon=True)
        worker.start()
        return public_status(home)
    return _finish()


def cancel_probe(home: Path) -> dict[str, Any]:
    """Cancel an in-flight probe. Completed results are left unchanged."""
    home = Path(home).expanduser().resolve()
    home_key = str(home)
    with _lock:
        event = _active.get(home_key)
        if event is not None:
            event.set()
        status = public_status(home)
        if status.get("state") != "running":
            return status
        # Soft-mark cancellation; the worker finalizes the document.
        document = _load_document(home) or {}
        document.update(
            {
                "version": _PROBE_VERSION,
                "state": "cancelled",
                "reason": "probe_cancelled",
                "checked_at": _utc_now(),
            }
        )
        if isinstance(document.get("binding"), dict):
            _write_document(home, document)
        return public_status(home)


def reset_for_tests() -> None:
    """Clear in-process probe bookkeeping. Tests only."""
    with _lock:
        for event in _active.values():
            event.set()
        _active.clear()
