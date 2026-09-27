"""ChatGPT Web session binding and loopback route lookup.

Gateway still owns the public Responses exchange. This module only resolves
the supervised runtime, projects doctor-listed models, and keeps the caller's
session, thread, turn, and item identities intact.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from catalog import canonical_model_id, compose_flat_label
from gateway_errors import identity_failure

import chatgpt_web_runtime

PROVIDER_ID = "chatgpt-web"
UPSTREAM_NAME = "chatgpt_web"
DISPLAY_PREFIX = "Web"
LOOPBACK_HOST = "127.0.0.1"

REASON_NOT_READY = "chatgpt_web_not_ready"
REASON_MODEL_NOT_LISTED = "chatgpt_web_model_not_listed"
REASON_PIN_INCOMPATIBLE = "chatgpt_web_pin_incompatible"
REASON_PROCESS_NOT_RUNNING = "chatgpt_web_process_not_running"

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
            }
        )
    return tuple(models)


def project_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Append doctor-listed web models. An empty doctor list adds nothing."""
    try:
        status = read_status()
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
            f"ChatGPT Web model is not in the runtime doctor list: {identity}",
            reason=REASON_MODEL_NOT_LISTED,
            provider_id=PROVIDER_ID,
            model_slug=identity,
        )
    return _upstream_facts(status, selected)


def ensure_exchange_allowed(upstream: Mapping[str, Any], payload: Mapping[str, Any] | None) -> None:
    """Re-check readiness and effort immediately before the upstream request is opened."""
    identity = canonical_model_id(str(upstream.get("model_id") or upstream.get("upstream_model") or ""))
    status = read_status()
    _raise_for_status(status, identity)
    selected = next((model for model in listed_models(status) if model["id"] == identity), None)
    if selected is None:
        raise identity_failure(
            f"ChatGPT Web model is not in the runtime doctor list: {identity}",
            reason=REASON_MODEL_NOT_LISTED,
            provider_id=PROVIDER_ID,
            model_slug=identity,
        )
    if payload is None:
        return
    requested = _requested_effort(payload)
    if requested is None:
        return
    if requested not in selected["efforts"]:
        raise identity_failure(
            f"ChatGPT Web effort is not in the runtime doctor list: {requested}",
            reason=REASON_MODEL_NOT_LISTED,
            provider_id=PROVIDER_ID,
            model_slug=identity,
        )


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
        signed_out = isinstance(login, Mapping) and login.get("state") != "signed_in"
        message = (
            "ChatGPT Web runtime is signed out and not ready"
            if signed_out
            else "ChatGPT Web runtime is not ready"
        )
        raise identity_failure(
            message,
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
    login = status.get("login")
    if not isinstance(login, Mapping) or login.get("state") != "signed_in":
        return False
    smoke = status.get("browser_smoke")
    return isinstance(smoke, Mapping) and smoke.get("state") == "passed"


def _upstream_facts(status: Mapping[str, Any], model: Mapping[str, Any]) -> dict[str, Any]:
    process = status.get("process")
    port = process.get("port") if isinstance(process, Mapping) else None
    host = process.get("listen_host") if isinstance(process, Mapping) else None
    if host != LOOPBACK_HOST or not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        raise identity_failure(
            "ChatGPT Web runtime is not ready",
            reason=REASON_NOT_READY,
            provider_id=PROVIDER_ID,
            model_slug=str(model["id"]),
        )
    token = _control_token()
    if not token:
        raise identity_failure(
            "ChatGPT Web runtime is not ready",
            reason=REASON_NOT_READY,
            provider_id=PROVIDER_ID,
            model_slug=str(model["id"]),
        )
    return {
        "name": UPSTREAM_NAME,
        "provider_id": PROVIDER_ID,
        "model_id": model["id"],
        "base_url": f"http://{LOOPBACK_HOST}:{port}",
        "auth": "api_key",
        "api_key": token,
        "upstream_model": model["id"],
        "upstream_format": "responses",
        "tool_protocol": "none",
        "tool_surface_strategy": "eager",
        "native_responses_tool_codec": "none",
        "reports_cached_input_tokens": False,
        "supports_developer_role": True,
        "supported_reasoning_levels": tuple(model["efforts"]),
        "input_modalities": ("text",),
    }


def _control_token() -> str:
    home = chatgpt_web_runtime.default_home()
    config = chatgpt_web_runtime._read_json(chatgpt_web_runtime._web_home(home) / "config.json") or {}
    token = config.get("controlToken")
    return token if isinstance(token, str) else ""


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
        "description": "ChatGPT Web model listed by the runtime doctor.",
        "visibility": "list",
        "supported_in_api": True,
        "input_modalities": ["text"],
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
