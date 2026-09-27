"""Resolve the ChatGPT Provider Connection to the owned local runtime."""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import hmac
import http.client
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Mapping

from catalog import canonical_model_id

import chatgpt_web_runtime

PROVIDER_ID = "chatgpt-web"
LOOPBACK_HOST = "127.0.0.1"


def selected_model_ids(status: Mapping[str, Any] | None = None) -> frozenset[str]:
    """Return Gateway-enabled models, preserving legacy auto-selection if unset."""
    from providers_config import load_providers

    provider = next(
        (item for item in load_providers() if canonical_model_id(item.id) == PROVIDER_ID),
        None,
    )
    if provider is None or not provider.enabled:
        return frozenset()
    if not provider.models:
        # The previous route exposed every account-visible model. Keep that
        # behavior until the user makes the first explicit model selection.
        raw_models = status.get("models") if isinstance(status, Mapping) else None
        return frozenset(
            canonical_model_id(item["id"])
            for item in raw_models
            if isinstance(item, Mapping)
            and isinstance(item.get("id"), str)
            and canonical_model_id(item["id"]).startswith(f"{PROVIDER_ID}/")
        ) if isinstance(raw_models, list) else frozenset()
    return frozenset(_qualified_model_id(model.id) for model in provider.models
                     if model.enabled and model.gateway_exported)


def provider_overrides() -> tuple[str, str]:
    """Read the optional manual connection overrides from providers.toml."""
    from providers_config import load_providers

    provider = next(
        (item for item in load_providers() if canonical_model_id(item.id) == PROVIDER_ID),
        None,
    )
    if provider is None:
        return "", ""
    return provider.base_url.strip(), provider.api_key.strip()


def check_connection(
    base_url: str = "",
    api_key: str = "",
    *,
    home: Path | None = None,
    status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Check saved/manual values without sending a credential off loopback."""
    runtime_home = chatgpt_web_runtime.default_home() if home is None else Path(home)
    current_status = (
        chatgpt_web_runtime.build_status(runtime_home) if status is None else status
    )
    resolved_url, resolved_key = resolve_connection(
        base_url,
        api_key,
        home=runtime_home,
        status=current_status,
    )
    endpoint = urlsplit(resolved_url)
    try:
        connection = http.client.HTTPConnection(LOOPBACK_HOST, endpoint.port, timeout=3)
        connection.request("GET", "/v1/models", headers={"Authorization": f"Bearer {resolved_key}"})
        response = connection.getresponse()
        body = response.read(1_048_577)
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError("ChatGPT Web service is not reachable on its managed loopback port") from exc
    finally:
        if "connection" in locals():
            connection.close()
    if response.status != 200:
        raise ValueError("ChatGPT Web service rejected the connection check")
    if len(body) > 1_048_576:
        raise ValueError("ChatGPT Web service returned an invalid model list")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("ChatGPT Web service returned an invalid model list") from exc
    model_rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(model_rows, list) or any(
        not isinstance(item, Mapping) or not isinstance(item.get("id"), str)
        for item in model_rows
    ):
        raise ValueError("ChatGPT Web service returned an invalid model list")
    return {
        "ok": True,
        "reachable": True,
        "base_url": resolved_url,
        "credential_configured": bool(resolved_key),
        "model_count": len(model_rows),
    }


def resolve_connection(
    base_url: str = "",
    api_key: str = "",
    *,
    home: Path | None = None,
    status: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve blank fields dynamically and constrain overrides to this process."""
    runtime_home = chatgpt_web_runtime.default_home() if home is None else Path(home)
    current_status = (
        chatgpt_web_runtime.build_status(runtime_home) if status is None else status
    )
    process = current_status.get("process")
    port = process.get("port") if isinstance(process, Mapping) else None
    if (
        current_status.get("installed") is not True
        or not isinstance(process, Mapping)
        or process.get("running") is not True
        or process.get("listen_host") != LOOPBACK_HOST
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        raise ValueError("ChatGPT Web Runtime is not running on its managed loopback endpoint")

    expected_url = f"http://{LOOPBACK_HOST}:{port}"
    expected_key = _service_key(runtime_home)
    if base_url and _validated_override_url(base_url, port) != expected_url:
        raise ValueError("Service address must match the current managed ChatGPT Web loopback endpoint")
    if api_key:
        try:
            matches = hmac.compare_digest(api_key.encode("utf-8"), expected_key.encode("utf-8"))
        except UnicodeEncodeError:
            matches = False
        if not matches:
            raise ValueError("Service access credential does not match the managed ChatGPT Web Runtime")
    return expected_url, api_key or expected_key


def _validated_override_url(value: str, current_port: int) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Service address is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != LOOPBACK_HOST
        or port != current_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Service address must match the current managed ChatGPT Web loopback endpoint")
    return f"http://{LOOPBACK_HOST}:{current_port}"


def _service_key(home: Path) -> str:
    try:
        config_path = chatgpt_web_runtime._web_home(home) / "config.json"
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    token = payload.get("controlToken") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("Managed ChatGPT Web service credential is unavailable")
    return token


def _qualified_model_id(model_id: str) -> str:
    identity = canonical_model_id(model_id)
    return identity if identity.startswith(f"{PROVIDER_ID}/") else f"{PROVIDER_ID}/{identity}"


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "check":
        print(json.dumps({"ok": False, "error": "unknown ChatGPT Web connection command"}))
        return 2
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("connection check input must be an object")
        result = check_connection(
            base_url=payload.get("base_url") if isinstance(payload.get("base_url"), str) else "",
            api_key=payload.get("api_key") if isinstance(payload.get("api_key"), str) else "",
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
