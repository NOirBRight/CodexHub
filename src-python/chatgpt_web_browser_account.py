"""Import a user-selected daily-browser ChatGPT session after live verification."""
from __future__ import annotations

from python_runtime_contract import require_python_313
require_python_313(__file__)

import math
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chatgpt_web_checks as checks
import chatgpt_web_runtime as runtime


def _cookies(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise ValueError("invalid_browser_session")
    result = []
    for cookie in value:
        if not isinstance(cookie, dict) or cookie.get("domain") not in {"chatgpt.com", ".chatgpt.com"}:
            raise ValueError("invalid_browser_session")
        name, content = cookie.get("name"), cookie.get("value")
        if (
            not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}", name)
            or not isinstance(content, str) or len(content) > 16384 or any(ord(c) < 32 for c in content)
            or cookie.get("secure") is not True or not isinstance(cookie.get("httpOnly"), bool)
            or cookie.get("path") != "/" or "partitionKey" in cookie
        ):
            raise ValueError("invalid_browser_session")
        expires = cookie.get("expirationDate", -1)
        if (
            isinstance(expires, bool) or not isinstance(expires, (int, float))
            or not math.isfinite(expires) or (expires != -1 and expires <= time.time())
        ):
            raise ValueError("invalid_browser_session")
        same_site = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}.get(cookie.get("sameSite"))
        if same_site is None:
            raise ValueError("invalid_browser_session")
        result.append({"name": name, "value": content, "domain": cookie["domain"],
                       "path": "/", "secure": True, "httpOnly": cookie["httpOnly"],
                       "expires": expires, "sameSite": same_site})
    return result


def import_session(home: Path, cookies: Any) -> dict[str, Any]:
    """Verify before selecting a new account; the running runtime keeps its active account."""
    state = {"cookies": _cookies(cookies), "origins": []}
    generation = secrets.token_hex(16)
    directory = home / "account" / generation
    selected = False
    try:
        status = runtime.build_status(home)
    except (OSError, RuntimeError) as exc:
        raise ValueError("component_start_required") from exc
    # The caller must explicitly restart to load a newly selected account.
    if not status.get("component", {}).get("compatible"):
        raise ValueError("component_upgrade_required")
    config = runtime._read_json(home / "web-home" / "config.json")
    if not config:
        raise ValueError("component_start_required")
    runtime._mkdir(directory)
    storage = directory / "storage-state.json"
    try:
        runtime._write_json(storage, state, mode=0o600)
        with checks._PinnedBrowserSession(home, {**config, "storageStatePath": str(storage)}) as browser:
            inspected = browser.operation("inspect", detect_capabilities=True).get("value") or {}
        if inspected.get("authenticated") is not True or inspected.get("temporary") is not True:
            raise ValueError("browser_sign_in_required")
        capabilities = inspected.get("capabilities") or {}
        marker = {"version": 1, "authenticated": True,
                  "verifiedAt": datetime.now(timezone.utc).isoformat(),
                  **{key: capabilities.get(key) is True for key in ("solAvailable", "extraHighAvailable", "proAvailable")}}
        runtime._write_json(storage.with_name(storage.name + ".verified.json"), marker, mode=0o600)
        with runtime._settings_lock(home):
            if not storage.is_file() or runtime._read_json(home / "web-home" / "config.json") != config:
                raise ValueError("browser_session_changed")
            runtime._write_json(home / "account" / "selected.json", {"generation": generation}, mode=0o600)
            selected = True
    except (OSError, RuntimeError) as exc:
        raise ValueError("browser_session_verification_failed") from exc
    finally:
        if not selected:
            shutil.rmtree(directory, ignore_errors=True)
    return {"ok": True, "authenticated": True, "restart_required": True}
