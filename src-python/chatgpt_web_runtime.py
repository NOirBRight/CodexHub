#!/usr/bin/env python3
"""CodexHub supervisor for the pinned ChatGPT Web Runtime.

Upstream ``setup`` always calls ``installCodexIntegration``. ``dev`` refuses
to start a Responses listener, and the upstream installer scripts are not
used. After the archive checksum matches, this module extracts it and starts
``bin/codex-chatgpt-web serve`` with config and homes kept inside the private
runtime directory. Upgrade downloads only that pinned archive. Disable stops
the supervisor and keeps the private account directory.
"""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import hashlib
import hmac
import io
import json
import math
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

PINNED_COMMIT = "a13cd09950969f43e3b7e25c71fa43efaf5446c5"
PINNED_VERSION = "6.1.1"
LOOPBACK_HOST = "127.0.0.1"
LOGIN_CONTROL = "owned-browser-v1"
RUNTIME_HEALTH_TIMEOUT_SECONDS = 2
RUNTIME_STARTUP_HEALTH_TIMEOUT_SECONDS = 30
RUNTIME_STARTUP_TIMEOUT_SECONDS = 180
MAX_STARTUP_DIAGNOSTIC_BYTES = 8192
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
REJECTED_ENTRIES = ("setup", "dev", "--replace-codex-route", "install.sh", "install-launcher.sh")
CONNECTOR_NAME = "Codex Native2"
ZERO_RISK_CONNECTOR_NAME = "Codex Zero Risk"
ENTRY_NAMES = ("bin/codex-chatgpt-web", "bin/codex-chatgpt-web.cmd")
ZERO_RISK_UNSUPPORTED_REASON = (
    "Zero Risk Pro requires manual browser interaction with the desktop launcher; "
    "the managed runtime uses automatic interaction with managed Chromium"
)
TUNNEL_CLIENT_VERSION = "0.0.12"
TUNNEL_CLIENT_ALLOWED_HOSTS = ALLOWED_DOWNLOAD_HOSTS | {"github.com"}
TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
TUNNEL_CONNECT_TIMEOUT_SECONDS = 125
TUNNEL_ID_PATTERN = re.compile(r"^tunnel_[a-f0-9]{32}$")
TUNNEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
RUNTIME_OPTION_DEFAULTS: dict[str, Any] = {
    "context_window": 256000,
    "headed": True,
    "auto_approve_tool_calls": False,
    "use_saved_chats": False,
    "experimental_bigger_context": False,
    "experimental_skill_attachments": False,
    "experimental_fresh_conversation_per_turn": False,
    "zero_risk_pro_enabled": False,
    "stall_timeout_sec": None,
}


class RuntimeError_(RuntimeError):
    """User-facing supervisor failure. Named to avoid shadowing the builtin."""


def artifact_key() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return "windows-x64"
    if sys.platform == "linux" and machine in {"x86_64", "amd64"}:
        return "linux-x64"
    if sys.platform == "linux" and machine in {"aarch64", "arm64"}:
        return "linux-arm64"
    raise RuntimeError_(f"unsupported ChatGPT Web Runtime platform: {sys.platform}/{machine}")


def pinned_launcher_url() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        name = "codex-web-gpt-6.1.1-win-x64.exe"
    elif machine in {"aarch64", "arm64"}:
        name = "codex-web-gpt-6.1.1-linux-arm64.AppImage"
    else:
        name = "codex-web-gpt-6.1.1-linux-x64.AppImage"
    return (
        "https://github.com/miuuyy/codex-chatgpt-web/releases/download/"
        f"v{PINNED_VERSION}/{name}"
    )


def default_pin_path() -> Path:
    override = os.environ.get("CODEXHUB_CHATGPT_WEB_PIN", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "config" / "chatgpt_web_runtime_pin.json"


def default_home() -> Path:
    override = os.environ.get("CODEXHUB_CHATGPT_WEB_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".codexhub" / "chatgpt-web"


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        return


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any], mode: int = 0o644) -> None:
    _mkdir(path.parent)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    try:
        os.chmod(path, mode)
    except OSError:
        return


def _write_private_bytes(path: Path, payload: bytes) -> None:
    _mkdir(path.parent)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _control_token(home: Path) -> str:
    config = _read_json(_web_home(home) / "config.json") or {}
    token = config.get("controlToken")
    return token if isinstance(token, str) else ""


def redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return _redact_token_prefix(redacted, "sk-")


def _redact_token_prefix(text: str, prefix: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        found = text.find(prefix, index)
        if found < 0:
            output.append(text[index:])
            break
        output.append(text[index:found])
        cursor = found + len(prefix)
        while cursor < len(text) and (text[cursor].isalnum() or text[cursor] in {"_", "-"}):
            cursor += 1
        if cursor - found > len(prefix) + 4:
            output.append("[redacted]")
            index = cursor
        else:
            output.append(text[found:cursor])
            index = cursor
    return "".join(output)


def _secrets(home: Path) -> list[str]:
    values = [_control_token(home)]
    settings = _read_json(_settings_path(home)) or {}
    tunnel = settings.get("tunnel")
    key = tunnel.get("runtime_key") if isinstance(tunnel, dict) else None
    if isinstance(key, str):
        values.append(key)
    config = _read_json(_web_home(home) / "config.json") or {}
    active_tunnel = config.get("tunnel")
    key_path = active_tunnel.get("runtimeKeyFile") if isinstance(active_tunnel, dict) else None
    if (
        isinstance(key_path, str)
        and Path(key_path).expanduser().resolve() == _managed_runtime_key(home).resolve()
    ):
        try:
            values.append(Path(key_path).read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return [value for value in values if value]


def _append_log(home: Path, message: str) -> None:
    _mkdir(home)
    line = redact(message, _secrets(home)).replace("\n", " ")
    with (home / "supervisor.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _assert_private_home(home: Path) -> Path:
    resolved = home.expanduser().resolve()
    forbidden: list[Path] = []
    for name in ("CODEX_HOME", "CODEX_WEB_GPT_DEV_HOME"):
        value = os.environ.get(name, "").strip()
        if value:
            forbidden.append(Path(value))
    user_home = Path.home()
    forbidden.extend(
        [
            user_home / ".codex",
            user_home / ".claude",
            user_home / ".config" / "opencode",
            user_home / ".codex-chatgpt-web-dev",
        ]
    )
    for item in forbidden:
        try:
            candidate = item.expanduser().resolve()
        except OSError:
            continue
        if resolved == candidate or candidate in resolved.parents or resolved in candidate.parents:
            raise RuntimeError_(
                "ChatGPT Web Runtime home must not overlap Codex, Claude, OpenCode, or DEV state"
            )
    return resolved


def _assert_pinned_url(url: str, version: str) -> None:
    parsed = urllib.parse.urlparse(url)
    expected = f"/miuuyy/codex-chatgpt-web/releases/download/v{version}/"
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise RuntimeError_("runtime download URL is not the pinned GitHub release")
    if parsed.username or parsed.password or "latest" in parsed.path:
        raise RuntimeError_("runtime download URL must be an exact release, not latest")
    if not parsed.path.startswith(expected):
        raise RuntimeError_("runtime download URL does not match the pinned version")


def load_pin(path: Path | None = None) -> dict[str, Any]:
    pin_path = path or default_pin_path()
    try:
        payload = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError_("ChatGPT Web Runtime pin is unreadable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError_("ChatGPT Web Runtime pin is invalid")
    if payload.get("commit") != PINNED_COMMIT or payload.get("version") != PINNED_VERSION:
        raise RuntimeError_(
            "incompatible ChatGPT Web Runtime pin "
            f"(expected {PINNED_VERSION} {PINNED_COMMIT})"
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or artifact_key() not in artifacts:
        raise RuntimeError_("ChatGPT Web Runtime pin has no artifact for this platform")
    artifact = artifacts[artifact_key()]
    if not isinstance(artifact, dict):
        raise RuntimeError_("ChatGPT Web Runtime pin artifact is invalid")
    sha = str(artifact.get("sha256") or "")
    if len(sha) != 64 or any(character not in "0123456789abcdef" for character in sha):
        raise RuntimeError_("ChatGPT Web Runtime pin checksum is invalid")
    bundled_filename = artifact.get("bundled_filename")
    if bundled_filename is not None and (
        not isinstance(bundled_filename, str)
        or not bundled_filename
        or bundled_filename in {".", ".."}
        or "/" in bundled_filename
        or "\\" in bundled_filename
        or ":" in bundled_filename
    ):
        raise RuntimeError_("bundled runtime must be a filename beside its pin")
    if artifact.get("bundled_only") is True:
        if not isinstance(bundled_filename, str):
            raise RuntimeError_("bundled-only runtime pin has no bundled archive")
        if artifact.get("url") is not None:
            raise RuntimeError_("bundled-only runtime pin cannot have a download URL")
    else:
        _assert_pinned_url(str(artifact.get("url") or ""), PINNED_VERSION)
    return payload


def _artifact(pin: dict[str, Any]) -> dict[str, Any]:
    artifact = pin["artifacts"][artifact_key()]
    if not isinstance(artifact, dict):
        raise RuntimeError_("ChatGPT Web Runtime pin artifact is invalid")
    return artifact


def _tunnel_artifact(pin: dict[str, Any]) -> dict[str, Any]:
    component = pin.get("tunnel_client")
    if not isinstance(component, dict) or component.get("version") != TUNNEL_CLIENT_VERSION:
        raise RuntimeError_("ChatGPT Web Tunnel client pin is missing or incompatible")
    artifacts = component.get("artifacts")
    artifact = artifacts.get(artifact_key()) if isinstance(artifacts, dict) else None
    if not isinstance(artifact, dict):
        raise RuntimeError_("ChatGPT Web Tunnel client pin has no artifact for this platform")
    filename = artifact.get("filename")
    expected_prefix = f"tunnel-client-v{TUNNEL_CLIENT_VERSION}-"
    if (
        not isinstance(filename, str)
        or not filename.startswith(expected_prefix)
        or not filename.endswith(".zip")
        or Path(filename).name != filename
    ):
        raise RuntimeError_("ChatGPT Web Tunnel client pin filename is invalid")
    url = artifact.get("url")
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    expected_path = f"/openai/tunnel-client/releases/download/v{TUNNEL_CLIENT_VERSION}/{filename}"
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.path != expected_path:
        raise RuntimeError_("ChatGPT Web Tunnel client URL is not the pinned release")
    digest = artifact.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError_("ChatGPT Web Tunnel client checksum is invalid")
    return artifact


def _runtime_root(home: Path) -> Path:
    return home / "current" / "runtime"


def _web_home(home: Path) -> Path:
    return home / "web-home"


def _broker_socket_path(web_home: Path) -> str:
    if sys.platform != "win32":
        return str(web_home / "socket" / "turn-broker.sock")
    identity = hashlib.sha256(str(web_home.resolve()).lower().encode("utf-8")).hexdigest()[:20]
    return rf"\\.\pipe\codex-chatgpt-web-{identity}"


def _settings_path(home: Path) -> Path:
    return home / "runtime-settings.json"


def _active_settings_path(home: Path) -> Path:
    return home / "active-runtime-settings.json"


def _settings_lock_path(home: Path) -> Path:
    return home / "runtime-settings.lock"


def _managed_tunnel_binary(home: Path) -> Path:
    return _web_home(home) / "bin" / ("tunnel-client.exe" if os.name == "nt" else "tunnel-client")


def _managed_runtime_key(home: Path) -> Path:
    return _web_home(home) / "secrets" / "tunnel-runtime-automatic.key"


def _managed_profile_dir(home: Path) -> Path:
    return _web_home(home) / "tunnel" / "profiles"


def _tunnel_alias_prefix(home: Path) -> str:
    identity = hashlib.sha256(str(home.resolve()).casefold().encode("utf-8")).hexdigest()[:12]
    return f"codexhub-{identity}-"


def _owned_tunnel_alias(home: Path, alias: str) -> str:
    return f"{_tunnel_alias_prefix(home)}{alias}"


def _codex_home(home: Path) -> Path:
    return home / "codex-home"


def _install_document(home: Path) -> dict[str, Any] | None:
    return _read_json(home / "current" / "install.json")


def _pin_compatible(home: Path, pin: dict[str, Any]) -> bool:
    installed = _install_document(home)
    if installed is None:
        return False
    artifact = _artifact(pin)
    return (
        installed.get("commit") == pin.get("commit") == PINNED_COMMIT
        and installed.get("version") == pin.get("version") == PINNED_VERSION
        and installed.get("sha256") == artifact.get("sha256")
        and installed.get("archive_executed") is False
    )


def _lifecycle(home: Path) -> dict[str, Any]:
    payload = _read_json(home / "lifecycle.json") or {}
    return {
        "enabled": payload.get("enabled") is not False,
        "restart_required": payload.get("restart_required") is True,
        "admitting": payload.get("admitting") is not False,
    }


def _write_lifecycle(
    home: Path,
    *,
    enabled: bool,
    restart_required: bool,
    admitting: bool = True,
) -> None:
    _write_json(
        home / "lifecycle.json",
        {
            "enabled": enabled,
            "restart_required": restart_required,
            "admitting": admitting,
        },
    )


def _account_dir(home: Path) -> Path:
    """Private login profile. Upgrade and disable never remove it."""
    return home / "account"


def _storage_state(home: Path) -> Path:
    return _web_home(home) / "browser" / "storage-state.json"


def _default_settings() -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "browser-only",
        "tunnel": {
            "tunnel_id": "",
            "profile_name": "codex-chatgpt-web",
            "alias": "codex-chatgpt-web",
            "runtime_key": "",
        },
        "connector_name": CONNECTOR_NAME,
        "options": dict(RUNTIME_OPTION_DEFAULTS),
    }


@contextmanager
def _settings_lock(home: Path) -> Iterator[None]:
    _mkdir(home)
    handle = _settings_lock_path(home).open("a+")
    deadline = time.monotonic() + 10
    try:
        while True:
            try:
                _try_lock(handle)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError_("ChatGPT Web settings are busy; retry the operation")
                time.sleep(0.01)
        yield
    finally:
        handle.close()


def _validate_settings(
    settings: dict[str, Any], *, require_full: bool, allow_unsupported_options: bool = False
) -> dict[str, Any]:
    if settings.get("version") != 1:
        raise RuntimeError_("ChatGPT Web settings version is unsupported")
    if settings.get("mode") not in {"browser-only", "full"}:
        raise RuntimeError_("ChatGPT Web mode must be browser-only or full")
    connector = settings.get("connector_name")
    if not isinstance(connector, str) or not connector.strip() or len(connector) > 80:
        raise RuntimeError_("ChatGPT connector name must contain 1 to 80 characters")
    if any(ord(character) < 32 for character in connector):
        raise RuntimeError_("ChatGPT connector name contains unsupported characters")
    tunnel = settings.get("tunnel")
    if not isinstance(tunnel, dict):
        raise RuntimeError_("ChatGPT Tunnel settings are invalid")
    tunnel_id = tunnel.get("tunnel_id")
    profile_name = tunnel.get("profile_name")
    alias = tunnel.get("alias")
    runtime_key = tunnel.get("runtime_key")
    if not isinstance(tunnel_id, str) or (tunnel_id and not TUNNEL_ID_PATTERN.fullmatch(tunnel_id)):
        raise RuntimeError_("ChatGPT Tunnel ID is invalid")
    if not isinstance(profile_name, str) or not TUNNEL_NAME_PATTERN.fullmatch(profile_name):
        raise RuntimeError_("ChatGPT Tunnel profile name is invalid")
    if not isinstance(alias, str) or len(alias) > 40 or not TUNNEL_NAME_PATTERN.fullmatch(alias):
        raise RuntimeError_("ChatGPT Tunnel alias is invalid")
    if not isinstance(runtime_key, str) or len(runtime_key.encode("utf-8")) > 64 * 1024:
        raise RuntimeError_("ChatGPT Tunnel Runtime Key is invalid")
    if require_full and settings["mode"] == "full" and (
        not tunnel_id or not runtime_key.strip()
    ):
        raise RuntimeError_("Full mode requires a Tunnel ID and Runtime Key")
    options = settings.get("options")
    if not isinstance(options, dict) or set(options) != set(RUNTIME_OPTION_DEFAULTS):
        raise RuntimeError_("ChatGPT runtime options are invalid")
    context_window = options.get("context_window")
    if (
        not isinstance(context_window, int)
        or isinstance(context_window, bool)
        or not 1 <= context_window <= 9_007_199_254_740_991
    ):
        raise RuntimeError_("ChatGPT context window must be a positive safe integer")
    for name in RUNTIME_OPTION_DEFAULTS.keys() - {"context_window", "stall_timeout_sec"}:
        if not isinstance(options.get(name), bool):
            raise RuntimeError_(f"ChatGPT option {name} must be true or false")
    if options["zero_risk_pro_enabled"] and not allow_unsupported_options:
        raise RuntimeError_(ZERO_RISK_UNSUPPORTED_REASON)
    stall_timeout = options.get("stall_timeout_sec")
    if stall_timeout is not None and (
        not isinstance(stall_timeout, (int, float))
        or isinstance(stall_timeout, bool)
        or not math.isfinite(stall_timeout)
        or stall_timeout <= 0
    ):
        raise RuntimeError_("ChatGPT stall timeout must be a positive number")
    return settings


def _settings_from_upstream(home: Path, config: dict[str, Any]) -> dict[str, Any]:
    settings = _default_settings()
    mode = config.get("mode")
    if mode in {"browser-only", "full"}:
        settings["mode"] = mode
    if config.get("browserInteractionMode", "automatic") != "automatic":
        raise RuntimeError_("This managed runtime supports the automatic connector mode only")
    connector = config.get("automaticAppName", config.get("appName", CONNECTOR_NAME))
    if isinstance(connector, str) and connector.strip():
        settings["connector_name"] = connector.strip()
    option_keys = {
        "context_window": "contextWindow",
        "headed": "headed",
        "auto_approve_tool_calls": "autoApproveToolCalls",
        "use_saved_chats": "useSavedChats",
        "experimental_bigger_context": "experimentalBiggerContext",
        "experimental_skill_attachments": "experimentalSkillAttachments",
        "experimental_fresh_conversation_per_turn": "experimentalFreshConversationPerTurn",
        "zero_risk_pro_enabled": "zeroRiskProEnabled",
        "stall_timeout_sec": "stallTimeoutSec",
    }
    for target, source in option_keys.items():
        if source in config:
            settings["options"][target] = config[source]
    tunnel = config.get("tunnel")
    if not isinstance(tunnel, dict) and isinstance(config.get("automaticTunnel"), dict):
        tunnel = config["automaticTunnel"]
    if isinstance(tunnel, dict):
        alias = tunnel.get("alias", "codex-chatgpt-web")
        if isinstance(alias, str) and alias.startswith(_tunnel_alias_prefix(home)):
            alias = alias.removeprefix(_tunnel_alias_prefix(home))
        settings["tunnel"].update(
            tunnel_id=tunnel.get("tunnelId", ""),
            profile_name=tunnel.get("profileName", "codex-chatgpt-web"),
            alias=alias,
        )
        expected_binary = _managed_tunnel_binary(home).resolve()
        expected_profile = _managed_profile_dir(home).resolve()
        binary = tunnel.get("binaryPath")
        profile = tunnel.get("profileDir")
        key_path = tunnel.get("runtimeKeyFile")
        if binary and Path(str(binary)).expanduser().resolve() != expected_binary:
            raise RuntimeError_("Existing ChatGPT Tunnel uses an unmanaged client path")
        if profile and Path(str(profile)).expanduser().resolve() != expected_profile:
            raise RuntimeError_("Existing ChatGPT Tunnel uses an unmanaged profile path")
        if key_path:
            expected_key = _managed_runtime_key(home).resolve()
            if Path(str(key_path)).expanduser().resolve() != expected_key:
                raise RuntimeError_("Existing ChatGPT Tunnel uses an unmanaged key path")
            try:
                settings["tunnel"]["runtime_key"] = expected_key.read_text(encoding="utf-8").strip()
            except OSError:
                settings["tunnel"]["runtime_key"] = ""
    return _validate_settings(settings, require_full=False, allow_unsupported_options=True)


def _read_saved_settings_locked(home: Path) -> dict[str, Any]:
    path = _settings_path(home)
    if path.exists():
        payload = _read_json(path)
        if payload is None:
            raise RuntimeError_("Saved ChatGPT Web settings are unreadable")
        return _validate_settings(payload, require_full=False, allow_unsupported_options=True)
    old_config = _read_json(_web_home(home) / "config.json")
    return _settings_from_upstream(home, old_config) if old_config else _default_settings()


def _runtime_key_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _persist_active_settings_locked(home: Path, settings: dict[str, Any]) -> None:
    saved = {
        **settings,
        "tunnel": {key: value for key, value in settings["tunnel"].items() if key != "runtime_key"},
    }
    _write_json(
        _active_settings_path(home),
        {
            "version": 1,
            "settings": saved,
            "runtime_key_sha256": _runtime_key_fingerprint(settings["tunnel"]["runtime_key"]),
        },
        mode=0o600,
    )


def _active_settings_locked(home: Path) -> dict[str, Any] | None:
    snapshot = _read_json(_active_settings_path(home))
    if snapshot and snapshot.get("version") == 1:
        settings = snapshot.get("settings")
        key_fingerprint = snapshot.get("runtime_key_sha256")
        if (
            isinstance(settings, dict)
            and isinstance(key_fingerprint, str)
            and re.fullmatch(r"[0-9a-f]{64}", key_fingerprint)
            and isinstance(settings.get("tunnel"), dict)
        ):
            active = {
                **settings,
                "tunnel": {
                    **settings["tunnel"],
                    "runtime_key": "active-runtime-key" if key_fingerprint != _runtime_key_fingerprint("") else "",
                },
                "_runtime_key_sha256": key_fingerprint,
            }
            try:
                validated = _validate_settings(active, require_full=False, allow_unsupported_options=True)
                validated["_runtime_key_sha256"] = key_fingerprint
                return validated
            except RuntimeError_:
                return None

    config = _read_json(_web_home(home) / "config.json")
    process = _process_record(home)
    if config is None or process is None or not _runtime_healthy(
        home,
        {"process": {"pid": process.get("pid"), "port": process.get("port")}},
    ):
        return None
    active = _settings_from_upstream(home, config)
    _persist_active_settings_locked(home, active)
    active["_runtime_key_sha256"] = _runtime_key_fingerprint(active["tunnel"]["runtime_key"])
    return active


def _active_settings_match(saved: dict[str, Any], active: dict[str, Any]) -> bool:
    return (
        _public_settings(saved) == _public_settings(active)
        and _runtime_key_fingerprint(saved["tunnel"]["runtime_key"])
        == active.get("_runtime_key_sha256")
    )


def _active_settings_match_config_locked(home: Path) -> bool:
    config = _read_json(_web_home(home) / "config.json")
    active = _active_settings_locked(home)
    return config is not None and active is not None and _active_settings_match(
        _settings_from_upstream(home, config), active
    )


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    tunnel = settings["tunnel"]
    unsupported_options = (
        {"zero_risk_pro_enabled": ZERO_RISK_UNSUPPORTED_REASON}
        if settings["options"]["zero_risk_pro_enabled"]
        else {}
    )
    return {
        "mode": settings["mode"],
        "configuration_complete": settings["mode"] != "full" or bool(
            tunnel["tunnel_id"] and tunnel["runtime_key"].strip()
        ),
        "connector_name": settings["connector_name"],
        "tunnel": {
            "tunnel_id": tunnel["tunnel_id"],
            "profile_name": tunnel["profile_name"],
            "alias": tunnel["alias"],
            "runtime_key_configured": bool(tunnel["runtime_key"]),
        },
        "options": dict(settings["options"]),
        "unsupported_options": unsupported_options,
    }


def _settings_snapshot_locked(home: Path) -> dict[str, Any]:
    saved = _read_saved_settings_locked(home)
    config_path = _web_home(home) / "config.json"
    active_state = "not_started"
    try:
        active = _active_settings_locked(home)
        if active is not None:
            active_state = "loaded"
        elif config_path.exists():
            active_state = "unavailable"
    except RuntimeError_:
        active = None
        active_state = "unavailable"
    return {
        "ok": True,
        "saved": _public_settings(saved),
        "active": None if active is None else _public_settings(active),
        "active_state": active_state,
        "pending_restart": (active is not None and not _active_settings_match(saved, active))
        or active_state == "unavailable",
        "restart_target": "ChatGPT Web Runtime",
    }


def read_settings(home: Path) -> dict[str, Any]:
    """Return saved and last-loaded runtime settings without exposing credentials."""
    home = _assert_private_home(home)
    with _settings_lock(home):
        return _settings_snapshot_locked(home)


def save_settings(home: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically save desired settings; never affect a running process."""
    home = _assert_private_home(home)
    if not isinstance(payload, dict):
        raise RuntimeError_("ChatGPT Web settings request must be an object")
    allowed = {"mode", "connector_name", "tunnel", "options"}
    if set(payload) - allowed:
        raise RuntimeError_("ChatGPT Web settings request contains unsupported fields")
    with _settings_lock(home):
        candidate = _read_saved_settings_locked(home)
        if "mode" in payload:
            candidate["mode"] = payload["mode"]
        if "connector_name" in payload:
            candidate["connector_name"] = payload["connector_name"]
        if "tunnel" in payload:
            update = payload["tunnel"]
            if not isinstance(update, dict) or set(update) - {
                "tunnel_id", "profile_name", "alias", "runtime_key"
            }:
                raise RuntimeError_("ChatGPT Tunnel settings request is invalid")
            for name in ("tunnel_id", "profile_name", "alias"):
                if name in update:
                    candidate["tunnel"][name] = update[name]
            if "runtime_key" in update:
                secret_update = update["runtime_key"]
                if not isinstance(secret_update, dict) or set(secret_update) - {"action", "value"}:
                    raise RuntimeError_("ChatGPT Runtime Key update is invalid")
                action = secret_update.get("action")
                if action == "keep" and set(secret_update) == {"action"}:
                    pass
                elif action == "clear" and set(secret_update) == {"action"}:
                    candidate["tunnel"]["runtime_key"] = ""
                elif action == "replace" and set(secret_update) == {"action", "value"}:
                    value = secret_update.get("value")
                    if not isinstance(value, str):
                        raise RuntimeError_("ChatGPT Runtime Key update is invalid")
                    candidate["tunnel"]["runtime_key"] = value.strip()
                else:
                    raise RuntimeError_("ChatGPT Runtime Key action must be keep, replace, or clear")
        if "options" in payload:
            updates = payload["options"]
            if not isinstance(updates, dict) or set(updates) - set(RUNTIME_OPTION_DEFAULTS):
                raise RuntimeError_("ChatGPT runtime options request is invalid")
            if updates.get("zero_risk_pro_enabled") is True:
                raise RuntimeError_(ZERO_RISK_UNSUPPORTED_REASON)
            candidate["options"].update(updates)
        _validate_settings(candidate, require_full=False, allow_unsupported_options=True)
        try:
            _write_json(_settings_path(home), candidate, mode=0o600)
        except OSError as exc:
            raise RuntimeError_("ChatGPT Web settings could not be saved") from exc
        result = _settings_snapshot_locked(home)
    result["changed"] = True
    return result


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split(maxsplit=1)[0]
            if state in {"Z", "X"}:
                return False
        except (OSError, IndexError):
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _cmdline(pid: int) -> str:
    if os.name == "nt":
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def _environ(pid: int) -> str:
    if os.name == "nt":
        return ""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b"\n").decode("utf-8", "replace")


def _process_record(home: Path) -> dict[str, Any] | None:
    payload = _read_json(home / "process.json")
    if not payload:
        return None
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not _pid_alive(pid) or not payload.get("executable"):
        return None
    if os.name == "nt":
        return payload
    # The bundle launcher execs Bun, so the cmdline no longer contains the
    # shell wrapper. The sandboxed homes remain in the process environment.
    command = _cmdline(pid).split()
    if "serve" not in command:
        return None
    if any(item in command for item in ("setup", "dev", "--replace-codex-route")):
        return None
    environ = set(_environ(pid).split("\n"))
    if f"CODEX_CHATGPT_WEB_HOME={_web_home(home)}" not in environ:
        return None
    if f"CODEX_HOME={_codex_home(home)}" not in environ:
        return None
    if any(item.startswith("CODEX_WEB_GPT_DEV_HOME=") for item in environ):
        return None
    return payload


def _reject_archive_path(name: str) -> None:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise RuntimeError_("runtime archive path escapes the install directory")


def _assert_link_inside(member_name: str, linkname: str) -> None:
    _reject_archive_path(member_name)
    link = PurePosixPath(linkname.replace("\\", "/"))
    if link.is_absolute():
        raise RuntimeError_("runtime archive link is absolute")
    parent = PurePosixPath(member_name.replace("\\", "/")).parent
    normalized = os.path.normpath(str(parent / linkname.replace("\\", "/")))
    if normalized.startswith("..") or normalized.startswith("/"):
        raise RuntimeError_("runtime archive link escapes the install directory")


def _extract_archive(archive: Path, dest: Path) -> None:
    _mkdir(dest)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                _reject_archive_path(info.filename)
            bundle.extractall(dest)
        return
    if not tarfile.is_tarfile(archive):
        raise RuntimeError_("runtime payload is not a tar.gz or zip archive")
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle.getmembers():
            _reject_archive_path(member.name)
            if member.issym() or member.islnk():
                _assert_link_inside(member.name, member.linkname or "")
        bundle.extractall(dest, filter="data")


def _find_entry(runtime_root: Path) -> Path:
    manifest = _read_json(runtime_root / "manifest.json") or {}
    launcher = manifest.get("launcher")
    relatives = []
    if isinstance(launcher, str) and launcher.strip():
        relatives.append(launcher.strip())
    relatives.extend(ENTRY_NAMES)
    for relative in relatives:
        _reject_archive_path(relative)
        candidate = (runtime_root / relative).resolve()
        if runtime_root.resolve() not in candidate.parents and candidate != runtime_root.resolve():
            continue
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise RuntimeError_("extracted runtime has no bin/codex-chatgpt-web entry")


def _restore_install(home: Path) -> None:
    current = home / "current"
    if current.exists():
        return
    for name in ("previous-good.next", "previous-good"):
        candidate = home / name
        if candidate.is_dir():
            os.rename(candidate, current)
            return


def _promote(home: Path, incoming: Path) -> None:
    _restore_install(home)
    current = home / "current"
    staged = home / "previous-good.next"
    if staged.exists():
        shutil.rmtree(staged)
    if current.exists():
        os.rename(current, staged)
        try:
            os.rename(incoming, current)
        except OSError:
            if not current.exists() and staged.exists():
                os.rename(staged, current)
            raise
        previous = home / "previous-good"
        if previous.exists():
            shutil.rmtree(previous)
        os.rename(staged, previous)
        return
    os.rename(incoming, current)


def _partial_marker(dest: Path) -> Path:
    return dest.with_name(dest.name + ".incomplete")


def _mark_partial(dest: Path) -> None:
    marker = _partial_marker(dest)
    marker.write_text("incomplete\n", encoding="utf-8")
    os.chmod(marker, 0o644)


def _clear_partial_marker(dest: Path) -> None:
    _partial_marker(dest).unlink(missing_ok=True)


def _discard_interrupted_partial(dest: Path) -> None:
    """Drop a killed download. The bytes are not extracted or executed."""
    if _partial_marker(dest).exists():
        dest.unlink(missing_ok=True)
        _clear_partial_marker(dest)


def _copy_to_partial(source: Path, dest: Path) -> None:
    if not source.is_file():
        raise RuntimeError_("runtime source is not a file")
    _mkdir(dest.parent)
    _discard_interrupted_partial(dest)
    _mark_partial(dest)
    try:
        with source.open("rb") as incoming, dest.open("wb") as outgoing:
            remaining = MAX_DOWNLOAD_BYTES
            for chunk in iter(lambda: incoming.read(1024 * 64), b""):
                remaining -= len(chunk)
                if remaining < 0:
                    raise RuntimeError_("runtime source exceeds the size limit")
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.chmod(dest, 0o644)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _download_to_partial(url: str, dest: Path, version: str) -> None:
    _assert_pinned_url(url, version)
    request = urllib.request.Request(url, headers={"User-Agent": "CodexHub"})
    _mkdir(dest.parent)
    _discard_interrupted_partial(dest)
    _mark_partial(dest)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                raise RuntimeError_("runtime download host is not pinned")
            if "latest" in final.path:
                raise RuntimeError_("runtime download URL must be an exact release, not latest")
            with dest.open("wb") as handle:
                remaining = MAX_DOWNLOAD_BYTES
                while True:
                    chunk = response.read(1024 * 64)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise RuntimeError_("runtime download exceeds the size limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        os.chmod(dest, 0o644)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _stage_verified_tree(home: Path, source: Path | None, pin: dict[str, Any]) -> Path:
    """Copy or download the pinned archive, verify it, and extract aside.

    A checksum failure or an interrupted partial is deleted and never executed.
    The current install is left in place. ``incoming`` is returned for promote.
    """
    artifact = _artifact(pin)
    if source is None and artifact.get("bundled_filename"):
        bundled_source = default_pin_path().parent / artifact["bundled_filename"]
        if artifact.get("bundled_only") is True and not bundled_source.is_file():
            raise RuntimeError_("required patched ChatGPT Web Runtime bundle is missing")
        source = bundled_source
    if source is None and artifact.get("bundled_only") is True:
        raise RuntimeError_("required patched ChatGPT Web Runtime bundle is not configured")
    partial = home / "staging" / "payload.partial"
    incoming = home / "incoming"
    try:
        _discard_interrupted_partial(partial)
        if incoming.exists():
            shutil.rmtree(incoming)
        if source is None:
            _download_to_partial(str(artifact["url"]), partial, PINNED_VERSION)
        else:
            _copy_to_partial(source, partial)
        if not partial.is_file() or not _partial_marker(partial).is_file():
            raise RuntimeError_("interrupted runtime download was not executed")
        digest = _sha256(partial)
        expected = str(artifact["sha256"])
        if not hmac.compare_digest(digest, expected):
            raise RuntimeError_("checksum mismatch; payload was not executed")
        os.chmod(partial, 0o644)
        _clear_partial_marker(partial)
        runtime_dest = incoming / "runtime"
        _extract_archive(partial, runtime_dest)
        entry = _find_entry(runtime_dest)
        os.chmod(entry, 0o755)
        payload_path = incoming / "payload"
        os.replace(partial, payload_path)
        os.chmod(payload_path, 0o644)
        _write_json(
            incoming / "install.json",
            {
                "commit": PINNED_COMMIT,
                "version": PINNED_VERSION,
                "sha256": digest,
                "filename": artifact.get("filename"),
                "build_revision": artifact.get("build_revision", PINNED_COMMIT),
                "archive_executed": False,
                "entry": str(entry.relative_to(runtime_dest)),
            },
        )
    except Exception:
        partial.unlink(missing_ok=True)
        _clear_partial_marker(partial)
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise
    return incoming


def install_runtime(home: Path, source: Path | None = None) -> dict[str, Any]:
    home = _assert_private_home(home)
    _mkdir(home)
    _restore_install(home)
    pin = load_pin()
    incoming = _stage_verified_tree(home, source, pin)
    try:
        _promote(home, incoming)
    except Exception:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise
    _append_log(home, "extracted pinned runtime; archive was not executed")
    _write_lifecycle(home, enabled=True, restart_required=False, admitting=True)
    return build_status(home, pin)


def _close_admission(home: Path) -> None:
    state = _lifecycle(home)
    _write_lifecycle(
        home,
        enabled=state["enabled"],
        restart_required=state["restart_required"],
        admitting=False,
    )


def _revoke_inflight_permits(home: Path) -> None:
    """Stop in-flight tool and collaboration permits before the install moves."""
    _write_json(home / "drain.json", {"id": secrets.token_hex(16), "reason": "upgrade"})
    import chatgpt_web_collab
    import chatgpt_web_client_session
    import chatgpt_web_route

    chatgpt_web_route.revoke_all_tool_permissions()
    chatgpt_web_collab.revoke_all_permissions()
    chatgpt_web_client_session.revoke_all_tool_permissions()


def _restore_after_failed_switch(home: Path, incoming: Path, previous: dict[str, Any], was_running: bool) -> None:
    if incoming.exists():
        shutil.rmtree(incoming, ignore_errors=True)
    _restore_install(home)
    _write_lifecycle(
        home,
        enabled=bool(previous["enabled"]),
        restart_required=bool(previous["restart_required"]) and not was_running,
        admitting=True,
    )
    if was_running:
        try:
            start_runtime(home)
        except Exception:
            _write_lifecycle(home, enabled=True, restart_required=True, admitting=False)
            _append_log(home, "previous good install was restored but did not restart")


def upgrade_runtime(home: Path, source: Path | None = None) -> dict[str, Any]:
    """Replace the install with the pinned archive only.

    Login files under the private home stay. A bad or interrupted download is
    not executed. Promotion failure puts the previous install back.
    """
    home = _assert_private_home(home)
    _mkdir(home)
    _restore_install(home)
    pin = load_pin()
    was_running = _process_record(home) is not None
    previous = _lifecycle(home)
    incoming = _stage_verified_tree(home, source, pin)
    _close_admission(home)
    try:
        _revoke_inflight_permits(home)
        if was_running:
            stop_runtime(home, disable=False)
        _promote(home, incoming)
    except Exception as exc:
        _restore_after_failed_switch(home, incoming, previous, was_running)
        if isinstance(exc, RuntimeError_):
            raise
        raise RuntimeError_("upgrade promotion failed; previous good install was kept") from exc
    _append_log(home, "upgraded pinned runtime; archive was not executed")
    _write_lifecycle(
        home,
        enabled=True,
        restart_required=was_running,
        admitting=not was_running,
    )
    return build_status(home, pin)


def delete_account(home: Path) -> dict[str, Any]:
    """Remove login files. Disable does not call this."""
    home = _assert_private_home(home)
    _mkdir(home)
    close_login(home)
    stop_runtime(home, disable=not _lifecycle(home)["enabled"])
    account = _account_dir(home)
    if account.is_symlink() or account.is_file():
        account.unlink()
    elif account.exists():
        shutil.rmtree(account)
    browser = _storage_state(home).parent
    if browser.exists():
        shutil.rmtree(browser)
    _append_log(home, "deleted ChatGPT Web account files")
    return build_status(home)


def _bind_host() -> str:
    requested = os.environ.get("CODEXHUB_CHATGPT_WEB_BIND", "").strip() or LOOPBACK_HOST
    if requested != LOOPBACK_HOST:
        raise RuntimeError_("ChatGPT Web Runtime status bind must be 127.0.0.1")
    return requested


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind((LOOPBACK_HOST, 0))
        return int(handle.getsockname()[1])


def _runtime_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_CHATGPT_WEB_HOME"] = str(_web_home(home))
    env["CODEX_HOME"] = str(_codex_home(home))
    env.pop("CODEX_WEB_GPT_DEV_HOME", None)
    return env


def _browser_executable() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
        if found := shutil.which(name):
            return found
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if root := os.environ.get(variable):
                for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                    candidate = Path(root) / relative
                    if candidate.is_file():
                        return str(candidate)
    # Preserve a concrete path for doctor to report as missing.
    return "/usr/bin/chromium" if os.name != "nt" else "chrome.exe"


def _write_minimum_config(home: Path, entry: Path) -> tuple[int, dict[str, Any]]:
    web_home = _web_home(home)
    with _settings_lock(home):
        settings = _validate_settings(_read_saved_settings_locked(home), require_full=True)
        web_config_path = web_home / "config.json"
        previous = _read_json(web_config_path) or {}
        _mkdir(web_home / "browser")
        if sys.platform != "win32":
            _mkdir(web_home / "socket")
        _mkdir(_codex_home(home))
        port = _free_port()
        old_token = previous.get("controlToken")
        token = (
            old_token
            if isinstance(old_token, str) and re.fullmatch(r"[A-Za-z0-9_-]{40,}", old_token)
            else secrets.token_urlsafe(48)
        )
        storage_state = _storage_state(home)
        capabilities = _read_json(storage_state.with_name(storage_state.name + ".verified.json")) or {}
        verified = (
            storage_state.is_file()
            and capabilities.get("version") == 1
            and capabilities.get("authenticated") is True
            and isinstance(capabilities.get("verifiedAt"), str)
        )
        sol_available = verified and capabilities.get("solAvailable") is True
        options = settings["options"]
        config: dict[str, Any] = {
            "version": 3,
            "releaseVersion": PINNED_VERSION,
            "mode": settings["mode"],
            "subagentProtocol": previous.get("subagentProtocol", "compatibility-v1"),
            "host": LOOPBACK_HOST,
            "port": port,
            "contextWindow": options["context_window"],
            "appName": settings["connector_name"],
            "automaticAppName": settings["connector_name"],
            "manualAppName": ZERO_RISK_CONNECTOR_NAME,
            "browserHost": "managed-chrome",
            "browserInteractionMode": "automatic",
            "chromeExecutablePath": _browser_executable(),
            "storageStatePath": str(storage_state),
            "brokerSocketPath": _broker_socket_path(web_home),
            "headed": options["headed"],
            "solAvailable": sol_available,
            "extraHighAvailable": sol_available and capabilities.get("extraHighAvailable") is True,
            "proAvailable": sol_available and capabilities.get("proAvailable") is True,
            "experimentalBiggerContext": options["experimental_bigger_context"],
            "experimentalSkillAttachments": options["experimental_skill_attachments"],
            "experimentalFreshConversationPerTurn": options["experimental_fresh_conversation_per_turn"],
            "useSavedChats": options["use_saved_chats"],
            "zeroRiskProEnabled": options["zero_risk_pro_enabled"],
            "autoApproveToolCalls": options["auto_approve_tool_calls"],
            "controlToken": token,
            "runtimeCommand": [str(entry)],
        }
        if options["stall_timeout_sec"] is not None:
            config["stallTimeoutSec"] = options["stall_timeout_sec"]
        if settings["mode"] == "full":
            tunnel = settings["tunnel"]
            key_path = _managed_runtime_key(home)
            _write_private_bytes(key_path, tunnel["runtime_key"].encode("utf-8"))
            config["tunnel"] = {
                "binaryPath": str(_managed_tunnel_binary(home)),
                "tunnelId": tunnel["tunnel_id"],
                "runtimeKeyFile": str(key_path),
                "profileDir": str(_managed_profile_dir(home)),
                "profileName": tunnel["profile_name"],
                "alias": _owned_tunnel_alias(home, tunnel["alias"]),
            }
        _write_json(web_config_path, config, mode=0o600)
    return port, settings


def _tunnel_manifest_path(home: Path) -> Path:
    return _managed_tunnel_binary(home).with_name("tunnel-client-manifest.json")


def _tunnel_ownership_path(home: Path) -> Path:
    return home / "tunnel-ownership.json"


def _download_tunnel_archive(artifact: dict[str, Any]) -> bytes:
    url = str(artifact["url"])
    request = urllib.request.Request(url, headers={"User-Agent": "CodexHub"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in TUNNEL_CLIENT_ALLOWED_HOSTS:
                raise RuntimeError_("Tunnel client download left the pinned release")
            length = response.headers.get("Content-Length")
            if length and int(length) > TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES:
                raise RuntimeError_("Tunnel client archive exceeds the size limit")
            payload = response.read(TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES + 1)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError_("Pinned Tunnel client download failed") from exc
    if len(payload) > TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES:
        raise RuntimeError_("Tunnel client archive exceeds the size limit")
    return payload


def _check_tunnel_version(binary: Path, home: Path) -> None:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=_runtime_env(home),
            cwd=str(binary.parent),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError_("Managed Tunnel client version check failed") from exc
    if result.returncode != 0 or TUNNEL_CLIENT_VERSION not in f"{result.stdout}\n{result.stderr}":
        raise RuntimeError_("Managed Tunnel client version check failed")


def _ensure_tunnel_client(home: Path, pin: dict[str, Any]) -> Path:
    artifact = _tunnel_artifact(pin)
    binary = _managed_tunnel_binary(home)
    manifest_path = _tunnel_manifest_path(home)
    if binary.is_symlink() or manifest_path.is_symlink():
        raise RuntimeError_("Managed Tunnel client path must not be a symbolic link")
    manifest = _read_json(manifest_path)
    if binary.is_file() and manifest is not None:
        digest = _sha256(binary)
        if (
            manifest.get("tunnelClientVersion") == TUNNEL_CLIENT_VERSION
            and manifest.get("asset") == artifact["filename"]
            and manifest.get("archiveSha256") == artifact["sha256"]
            and manifest.get("binarySha256") == digest
            and (os.name == "nt" or binary.stat().st_mode & 0o111)
        ):
            _check_tunnel_version(binary, home)
            return binary
    payload = _download_tunnel_archive(artifact)
    if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
        raise RuntimeError_("Tunnel client archive checksum mismatch")
    expected_name = "tunnel-client.exe" if os.name == "nt" else "tunnel-client"
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            matches = [
                entry for entry in archive.infolist()
                if PurePosixPath(entry.filename.replace("\\", "/")).name == expected_name
            ]
            if len(matches) != 1 or stat.S_ISLNK(matches[0].external_attr >> 16):
                raise RuntimeError_("Pinned Tunnel archive has no unique regular client binary")
            if matches[0].file_size < 1 or matches[0].file_size > TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES:
                raise RuntimeError_("Pinned Tunnel client binary has an invalid size")
            binary_payload = archive.read(matches[0])
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, RuntimeError_):
            raise
        raise RuntimeError_("Pinned Tunnel client archive is invalid") from exc
    if not binary_payload:
        raise RuntimeError_("Pinned Tunnel client binary is empty")
    staged = binary.with_name(f"{binary.name}.install-{os.getpid()}-{secrets.token_hex(8)}")
    try:
        _write_private_bytes(staged, binary_payload)
        if os.name != "nt":
            os.chmod(staged, 0o700)
        _check_tunnel_version(staged, home)
        _mkdir(binary.parent)
        os.replace(staged, binary)
        if os.name != "nt":
            os.chmod(binary, 0o700)
        _write_json(
            manifest_path,
            {
                "version": 1,
                "tunnelClientVersion": TUNNEL_CLIENT_VERSION,
                "asset": artifact["filename"],
                "archiveSha256": artifact["sha256"],
                "binarySha256": hashlib.sha256(binary_payload).hexdigest(),
            },
            mode=0o600,
        )
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return binary


def _tunnel_config(home: Path) -> dict[str, str]:
    config = _read_json(_web_home(home) / "config.json") or {}
    tunnel = config.get("tunnel")
    if config.get("mode") != "full" or not isinstance(tunnel, dict):
        raise RuntimeError_("Full mode has no managed Tunnel configuration")
    expected_paths = {
        "binaryPath": _managed_tunnel_binary(home),
        "runtimeKeyFile": _managed_runtime_key(home),
        "profileDir": _managed_profile_dir(home),
    }
    for name, expected in expected_paths.items():
        value = tunnel.get(name)
        if not isinstance(value, str) or Path(value).expanduser().resolve() != expected.resolve():
            raise RuntimeError_("ChatGPT Tunnel configuration uses an unmanaged path")
    tunnel_id = tunnel.get("tunnelId")
    profile_name = tunnel.get("profileName")
    alias = tunnel.get("alias")
    if (
        not isinstance(tunnel_id, str) or not TUNNEL_ID_PATTERN.fullmatch(tunnel_id)
        or not isinstance(profile_name, str) or not TUNNEL_NAME_PATTERN.fullmatch(profile_name)
        or not isinstance(alias, str) or not alias.startswith(_tunnel_alias_prefix(home))
    ):
        raise RuntimeError_("ChatGPT Tunnel configuration is invalid")
    return {
        "binary": str(expected_paths["binaryPath"]),
        "tunnel_id": tunnel_id,
        "key": str(expected_paths["runtimeKeyFile"]),
        "profile_dir": str(expected_paths["profileDir"]),
        "profile_name": profile_name,
        "alias": alias,
    }


def _tunnel_mcp_command(entry: Path, broker_socket: str, contract: str = "native") -> str:
    if contract not in {"native", "safe"}:
        raise RuntimeError_("Unsupported Tunnel MCP contract")
    command = [str(entry), "mcp", "--contract", contract, "--broker-socket", str(broker_socket)]
    if any("\r" in item or "\n" in item for item in command):
        raise RuntimeError_("Tunnel MCP command contains a newline")
    return " ".join('"' + item.replace("\\", "\\\\").replace('"', '\\"') + '"' for item in command)


def _run_tunnel_command(
    home: Path,
    tunnel: dict[str, str],
    args: list[str],
    *,
    timeout: float,
) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            [tunnel["binary"], *args],
            check=False,
            capture_output=True,
            text=True,
            env=_runtime_env(home),
            cwd=tunnel["profile_dir"],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = _tunnel_error_output(home, _captured_text(exc.stdout), _captured_text(exc.stderr))
        detail = f": {output}" if output else ""
        raise RuntimeError_(f"Managed Tunnel client command timed out{detail}") from exc
    except OSError as exc:
        raise RuntimeError_("Managed Tunnel client command failed") from exc
    return result.returncode, result.stdout, result.stderr


def _captured_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _tunnel_error_output(home: Path, stdout: str, stderr: str) -> str:
    return redact(f"{stderr}\n{stdout}", _secrets(home)).strip()[:2000]


def _tunnel_inventory(home: Path, tunnel: dict[str, str]) -> dict[str, Any]:
    code, stdout, stderr = _run_tunnel_command(home, tunnel, ["runtimes", "cleanup", "--json"], timeout=10)
    if code != 0:
        output = _tunnel_error_output(home, stdout, stderr)
        raise RuntimeError_(f"Tunnel runtime inventory failed: {output or f'exit {code}'}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError_("Tunnel runtime inventory returned invalid JSON") from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError_("Tunnel runtime inventory has no entries")
    matches = [item for item in entries if isinstance(item, dict) and item.get("alias") == tunnel["alias"]]
    if len(matches) > 1:
        raise RuntimeError_("Tunnel runtime inventory contains a duplicate owned alias")
    if not matches:
        return {"state": "stopped", "pid": None}
    entry = matches[0]
    state = entry.get("runtime_state")
    if state not in {"stopped", "starting", "healthy", "ready"}:
        raise RuntimeError_("Tunnel runtime inventory returned an unknown state")
    return {"state": state}


def _tunnel_process_status(home: Path, tunnel: dict[str, str]) -> dict[str, Any]:
    code, stdout, stderr = _run_tunnel_command(
        home, tunnel, ["runtimes", "status", tunnel["alias"], "--json"], timeout=10
    )
    if code != 0:
        output = _tunnel_error_output(home, stdout, stderr)
        raise RuntimeError_(f"Tunnel runtime status failed: {output or f'exit {code}'}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError_("Tunnel runtime status returned invalid JSON") from exc
    process = payload.get("process") if isinstance(payload, dict) else None
    if (
        not isinstance(process, dict)
        or payload.get("alias") != tunnel["alias"]
        or payload.get("tunnel_id") != tunnel["tunnel_id"]
        or Path(str(process.get("profile_dir") or "")).expanduser().resolve()
        != Path(tunnel["profile_dir"]).resolve()
    ):
        raise RuntimeError_("Tunnel runtime status does not match the managed alias and profile")
    mode = process.get("mode")
    pid = process.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        pid = None
    session_name = process.get("session_name")
    if not isinstance(session_name, str) or not session_name:
        session_name = None
    if (mode == "process" and pid is None) or (mode == "tmux" and session_name is None):
        raise RuntimeError_("Tunnel runtime status has no verifiable process identity")
    if mode not in {"process", "tmux"}:
        raise RuntimeError_("Tunnel runtime status has an unknown process mode")
    running = payload.get("process_running") is True
    if mode == "tmux":
        tmux = payload.get("tmux")
        running = running or (isinstance(tmux, dict) and tmux.get("running") is True)
    if not running:
        raise RuntimeError_("Tunnel runtime process is not running")
    return {"pid": pid, "mode": mode, "session_name": session_name}


def _same_tunnel_owner(
    marker: dict[str, Any] | None,
    tunnel: dict[str, str],
    status: dict[str, Any],
) -> bool:
    if not (
        marker
        and marker.get("owner") == "codexhub"
        and marker.get("alias") == tunnel["alias"]
        and marker.get("tunnel_id") == tunnel["tunnel_id"]
        and marker.get("profile_dir") == tunnel["profile_dir"]
        and marker.get("mode") == status.get("mode")
    ):
        return False
    if status["mode"] == "process":
        return isinstance(status.get("pid"), int) and marker.get("pid") == status["pid"]
    return isinstance(status.get("session_name"), str) and marker.get("session_name") == status["session_name"]


def _write_tunnel_owner(
    home: Path,
    tunnel: dict[str, str],
    identity: dict[str, Any],
    *,
    start_id: str | None = None,
) -> None:
    marker = {
        "owner": "codexhub",
        "alias": tunnel["alias"],
        "tunnel_id": tunnel["tunnel_id"],
        "profile_dir": tunnel["profile_dir"],
        **identity,
    }
    if start_id is not None:
        marker["start_id"] = start_id
    _write_json(_tunnel_ownership_path(home), marker, mode=0o600)


def _stop_tunnel_runtime(home: Path, tunnel: dict[str, str]) -> None:
    status = _tunnel_inventory(home, tunnel)
    marker_path = _tunnel_ownership_path(home)
    marker = _read_json(marker_path)
    if status["state"] == "stopped":
        marker_path.unlink(missing_ok=True)
        return
    identity = _tunnel_process_status(home, tunnel)
    if not _same_tunnel_owner(marker, tunnel, identity):
        raise RuntimeError_("Refusing to stop an unowned Tunnel runtime alias")
    code, stdout, stderr = _run_tunnel_command(
        home, tunnel, ["runtimes", "stop", tunnel["alias"], "--json"], timeout=10
    )
    output = _tunnel_error_output(home, stdout, stderr)
    if code != 0 and not re.search(r"not found|not running|unknown alias", output, re.IGNORECASE):
        raise RuntimeError_(f"Tunnel runtime refused shutdown: {output or f'exit {code}'}")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = _tunnel_inventory(home, tunnel)
        if status["state"] == "stopped":
            marker_path.unlink(missing_ok=True)
            return
        time.sleep(0.25)
    raise RuntimeError_("Tunnel runtime did not confirm stopped state")


def _start_tunnel_runtime(home: Path, pin: dict[str, Any], entry: Path) -> dict[str, str]:
    tunnel = _tunnel_config(home)
    _ensure_tunnel_client(home, pin)
    Path(tunnel["profile_dir"]).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(tunnel["profile_dir"], 0o700)
    except OSError:
        pass
    previous = _tunnel_inventory(home, tunnel)
    if previous["state"] != "stopped":
        marker = _read_json(_tunnel_ownership_path(home))
        identity = _tunnel_process_status(home, tunnel)
        if not _same_tunnel_owner(marker, tunnel, identity):
            raise RuntimeError_("Tunnel alias is already in use by an unowned runtime")
        _stop_tunnel_runtime(home, tunnel)
    else:
        _tunnel_ownership_path(home).unlink(missing_ok=True)
    start_id = secrets.token_hex(16)
    _write_json(
        _tunnel_ownership_path(home),
        {
            "owner": "codexhub",
            "alias": tunnel["alias"],
            "tunnel_id": tunnel["tunnel_id"],
            "profile_dir": tunnel["profile_dir"],
            "start_id": start_id,
        },
        mode=0o600,
    )
    config = _read_json(_web_home(home) / "config.json") or {}
    contract = "safe" if config.get("browserInteractionMode") == "manual" else "native"
    args = [
        "runtimes", "connect",
        "--alias", tunnel["alias"],
        "--profile", tunnel["profile_name"],
        "--profile-dir", tunnel["profile_dir"],
        "--tunnel-client-bin", tunnel["binary"],
        "--tunnel-id", tunnel["tunnel_id"],
        "--runtime-api-key", f"file:{tunnel['key']}",
        "--mcp-command", _tunnel_mcp_command(entry, config["brokerSocketPath"], contract),
        "--json",
    ]
    try:
        code, stdout, stderr = _run_tunnel_command(
            home, tunnel, args, timeout=TUNNEL_CONNECT_TIMEOUT_SECONDS
        )
        try:
            result = json.loads(stdout) if stdout else None
        except json.JSONDecodeError as exc:
            raise RuntimeError_("Tunnel managed startup returned invalid JSON") from exc
        if code != 0:
            output = _tunnel_error_output(home, stdout, stderr)
            raise RuntimeError_(f"Tunnel managed startup failed: {output or f'exit {code}'}")
        if not isinstance(result, dict) or result.get("running") is not True or result.get("healthy") is not True:
            raise RuntimeError_("Tunnel managed startup did not confirm a healthy process")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = _tunnel_inventory(home, tunnel)
            if status["state"] == "ready":
                identity = _tunnel_process_status(home, tunnel)
                _write_tunnel_owner(home, tunnel, identity, start_id=start_id)
                return tunnel
            if status["state"] == "stopped":
                raise RuntimeError_("Tunnel runtime stopped during startup")
            time.sleep(1)
        raise RuntimeError_("Tunnel runtime did not reach ready state within 120 seconds")
    except Exception:
        try:
            _cleanup_tunnel_start(home, tunnel, start_id)
        except Exception as cleanup_error:
            _append_log(home, f"tunnel startup cleanup failed: {cleanup_error}")
        raise


def _cleanup_tunnel_start(home: Path, tunnel: dict[str, str], start_id: str) -> None:
    marker_path = _tunnel_ownership_path(home)
    marker = _read_json(marker_path)
    if not marker or marker.get("start_id") != start_id:
        raise RuntimeError_("Refusing Tunnel startup cleanup without matching ownership marker")
    status = _tunnel_inventory(home, tunnel)
    if status["state"] == "stopped":
        marker_path.unlink(missing_ok=True)
        return
    identity = _tunnel_process_status(home, tunnel)
    _write_tunnel_owner(home, tunnel, identity, start_id=start_id)
    _stop_tunnel_runtime(home, tunnel)


def _run_doctor(home: Path, entry: Path) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [str(entry), "doctor", "--json"],
            check=False,
            capture_output=True,
            text=True,
            env=_runtime_env(home),
            cwd=str(_web_home(home)),
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        _append_log(home, "runtime doctor did not return")
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        _append_log(home, "runtime doctor did not return JSON")
        return None
    if not isinstance(payload, dict):
        return None
    return _redact_diagnostic_fields(payload, _secrets(home))


def _redact_diagnostic_fields(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: redact(item, secrets)
            if key in {"message", "error", "detail"} and isinstance(item, str)
            else _redact_diagnostic_fields(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_diagnostic_fields(item, secrets) for item in value]
    return value


def _doctor_lists_image(value: dict[str, Any]) -> bool:
    """True only when the doctor row lists image input. Missing means text only."""
    modalities = value.get("input_modalities", value.get("modalities"))
    if isinstance(modalities, list):
        for item in modalities:
            if isinstance(item, str) and item.strip().lower() == "image":
                return True
    capabilities = value.get("capabilities")
    if isinstance(capabilities, list):
        for item in capabilities:
            if isinstance(item, str) and item.strip().lower() in {"image", "image_input", "vision"}:
                return True
    return value.get("image_input") is True or value.get("supports_image_input") is True


def normalize_runtime_model(value: Any) -> dict[str, Any] | None:
    """Normalize a model row from either the runtime doctor or its API."""
    if isinstance(value, str):
        model_id = value.strip()
        efforts: list[Any] = []
        display_name = ""
        image_input = False
    elif isinstance(value, dict):
        raw_id = value.get("id", value.get("slug"))
        model_id = raw_id.strip() if isinstance(raw_id, str) else ""
        display_name = value.get("display_name", value.get("name"))
        display_name = display_name.strip() if isinstance(display_name, str) else ""
        raw_efforts = value.get("efforts", value.get("supported_reasoning_levels"))
        efforts = raw_efforts if isinstance(raw_efforts, list) else []
        image_input = _doctor_lists_image(value)
    else:
        return None
    if not model_id.startswith("chatgpt-web/"):
        return None
    parsed_efforts: list[str] = []
    for effort in efforts:
        if isinstance(effort, str):
            token = effort.strip().lower()
        elif isinstance(effort, dict) and isinstance(effort.get("effort"), str):
            token = effort["effort"].strip().lower()
        else:
            continue
        if token and token not in parsed_efforts:
            parsed_efforts.append(token)
    return {
        "id": model_id,
        "display_name": display_name,
        "efforts": parsed_efforts,
        "image_input": image_input,
    }


def build_status(home: Path, pin: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        settings_status = read_settings(home)
    except RuntimeError_:
        settings_status = {"pending_restart": True, "restart_target": "ChatGPT Web Runtime"}
    loaded = pin or load_pin()
    artifact = _artifact(loaded)
    installed = _install_document(home)
    compatible = _pin_compatible(home, loaded)
    record = _process_record(home)
    running = record is not None
    from chatgpt_web_checks import cached_checks

    checks = cached_checks(home)
    checks_current = checks.get("cache_state") == "current"
    login = checks.get("login")
    browser = checks.get("browser")
    tunnel = checks.get("tunnel")
    connector = checks.get("connector")
    login_state = (
        login.get("state")
        if checks_current and isinstance(login, dict) and login.get("state") in {"signed_in", "signed_out"}
        else "unknown"
    )
    layers = {
        "login": login_state,
        "browser_smoke": (
            "passed" if checks_current and isinstance(browser, dict) and browser.get("state") == "passed"
            else "failed" if checks_current and isinstance(browser, dict) and browser.get("state") == "failed"
            else "not_run"
        ),
        "tunnel": (
            "ready" if checks_current and isinstance(tunnel, dict) and tunnel.get("state") == "ready"
            else "failed" if checks_current and isinstance(tunnel, dict) and tunnel.get("state") == "not_ready"
            else "not_started"
        ),
        "connector_selectable": (
            checks_current and isinstance(connector, dict) and connector.get("state") == "selectable"
        ),
    }
    check_models = checks.get("models")
    models = check_models if checks_current and isinstance(check_models, list) else []
    capabilities_match = checks.get("capabilities_match") is True if checks_current else False
    lifecycle = _lifecycle(home)
    window = _read_json(home / "window.json") or {}
    window_error = window.get("error")
    if isinstance(window_error, str):
        window_error = redact(window_error, _secrets(home))
    restart_required = lifecycle["restart_required"] or bool(
        running and record.get("login_control") != LOGIN_CONTROL
    )
    ready = bool(
        compatible
        and running
        and lifecycle["enabled"]
        and not restart_required
        and layers["login"] == "signed_in"
        and layers["browser_smoke"] == "passed"
        and capabilities_match
        and bool(models)
        and layers["tunnel"] == "ready"
        and layers["connector_selectable"] is True
    )
    executable = None if record is None else record.get("executable")
    return {
        "ok": True,
        "component": {
            "version": PINNED_VERSION,
            "commit": PINNED_COMMIT,
            "pin": PINNED_COMMIT,
            "artifact_sha256": artifact.get("sha256"),
            "installed_sha256": None if installed is None else installed.get("sha256"),
            "compatible": compatible,
        },
        "login": {
            "state": layers["login"],
            "window": "open" if running and window.get("open") is True else "closed",
            "error": window_error or (login.get("reason") if isinstance(login, dict) else None),
            "control": None if record is None else record.get("login_control"),
            "account_id": None,
        },
        "browser_smoke": {"state": layers["browser_smoke"]},
        "tunnel": {"state": layers["tunnel"], "detail": ""},
        "connector": {"selectable": layers["connector_selectable"]},
        "process": {
            "pid": None if record is None else record.get("pid"),
            "port": None if record is None else record.get("port"),
            "diagnostic_port": None if record is None else record.get("diagnostic_port"),
            "executable": executable,
            "private_home": str(home),
            "running": running,
            "ownership": "codexhub-supervisor",
            "listen_host": LOOPBACK_HOST if running else None,
        },
        "readiness_checks": {
            key: checks.get(key)
            for key in (
                "state", "cache_state", "checked_at", "reason",
                "runtime_capabilities", "capabilities_match",
            )
        },
        "ready": ready,
        "capacity": "available",
        "models": models,
        "restart_required": restart_required,
        "settings_pending_restart": settings_status["pending_restart"],
        "settings_restart_target": settings_status["restart_target"],
        "admitting": lifecycle["admitting"],
        "disabled": not lifecycle["enabled"],
        "installed": installed is not None,
        "entry": "codex-chatgpt-web serve",
        "upstream_executed": False,
        "rejected_entries": list(REJECTED_ENTRIES),
    }


def _diagnostic_page() -> bytes:
    url = pinned_launcher_url()
    page = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>ChatGPT Web Runtime</title></head>"
        "<body><h1>ChatGPT Web Runtime</h1>"
        "<p>This diagnostic page is not a ChatGPT login. It does not collect a secret "
        "and it does not mark the runtime signed in.</p>"
        "<p>The login window is the pinned Codex Web GPT launcher for this release. "
        "CodexHub does not run upstream setup, dev, or the installer scripts.</p>"
        f"<p><a href=\"{url}\">Pinned launcher v{PINNED_VERSION}</a></p>"
        "<p>Login, browser smoke, tunnel, and connector stay unready until an "
        "explicit readiness check verifies them.</p></body></html>"
    )
    return page.encode("utf-8")


class _StatusHandler(BaseHTTPRequestHandler):
    server_version = "CodexHubChatGPTWeb/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        home = getattr(self.server, "home", None)
        if isinstance(home, Path):
            _append_log(home, fmt % args)

    def _client_allowed(self) -> bool:
        return self.client_address[0] == LOOPBACK_HOST

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._client_allowed():
            self._send(403, b'{"ok":false,"error":"loopback only"}', "application/json")
            return
        path = self.path.split("?", 1)[0]
        home: Path = self.server.home  # type: ignore[attr-defined]
        if path == "/status":
            text = json.dumps(build_status(home), sort_keys=True)
            self._send(200, text.encode("utf-8"), "application/json")
            return
        if path == "/login":
            self._send(200, _diagnostic_page(), "text/html; charset=utf-8")
            return
        self._send(404, b'{"ok":false,"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        self._send(404, b'{"ok":false,"error":"not found"}', "application/json")


def _emit(home: Path, payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _fail(home: Path, message: str) -> int:
    safe = redact(message, _secrets(home))
    try:
        _append_log(home, safe)
    except OSError:
        pass
    sys.stdout.write(json.dumps({"ok": False, "error": safe}, sort_keys=True) + "\n")
    return 1


def _startup_fail(home: Path, message: str) -> int:
    _write_startup_diagnostic(home, message)
    return _fail(home, message)


def supervise(home: Path) -> int:
    home = _assert_private_home(home)
    _mkdir(home)
    pin = load_pin()
    if not _pin_compatible(home, pin):
        return _startup_fail(
            home,
            "version mismatch; refusing to start an incompatible ChatGPT Web Runtime pin",
        )
    if not _lifecycle(home)["enabled"]:
        return _startup_fail(home, "ChatGPT Web Runtime is disabled")
    host = _bind_host()
    try:
        entry = _find_entry(_runtime_root(home))
    except RuntimeError_ as exc:
        return _startup_fail(home, str(exc))
    from chatgpt_web_login import LoginSession

    lock_handle = (home / "supervisor.lock").open("a+")
    try:
        _try_lock(lock_handle)
    except BlockingIOError:
        lock_handle.close()
        return _startup_fail(home, "ChatGPT Web Runtime supervisor is already running")
    child = None
    child_output = bytearray()
    child_output_lock = threading.Lock()
    child_output_reader = None
    startup_healthy = threading.Event()
    server = None
    thread = None
    login = None
    tunnel = None
    startup_error: Exception | None = None
    try:
        runtime_port, startup_settings = _write_minimum_config(home, entry)
        config = _read_json(_web_home(home) / "config.json") or {}
        if config.get("mode") == "full":
            tunnel = _start_tunnel_runtime(home, pin, entry)
        child = subprocess.Popen(
            [str(entry), "serve"],
            env=_runtime_env(home),
            cwd=str(_web_home(home)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        def _capture_startup_output() -> None:
            assert child is not None and child.stdout is not None
            while chunk := child.stdout.read1(1024):
                with child_output_lock:
                    if startup_healthy.is_set():
                        continue
                    child_output.extend(chunk)
                    if len(child_output) > MAX_STARTUP_DIAGNOSTIC_BYTES:
                        del child_output[:-MAX_STARTUP_DIAGNOSTIC_BYTES]

        child_output_reader = threading.Thread(target=_capture_startup_output, daemon=True)
        child_output_reader.start()
        server = ThreadingHTTPServer((host, 0), _StatusHandler)
        bound_host, diagnostic_port = server.server_address[:2]
        if bound_host != LOOPBACK_HOST:
            raise RuntimeError_("refusing to listen outside 127.0.0.1")
        login = LoginSession(home, entry)
        server.home = home  # type: ignore[attr-defined]
        tunnel_owner = _read_json(_tunnel_ownership_path(home)) or {}
        _write_json(
            home / "process.json",
            {
                "pid": child.pid,
                "supervisor_pid": os.getpid(),
                "login_control": LOGIN_CONTROL,
                "port": runtime_port,
                "diagnostic_port": diagnostic_port,
                "executable": str(entry),
                "private_home": str(home),
                "ownership": "codexhub-supervisor",
                "tunnel_alias": tunnel.get("alias") if tunnel else None,
                "tunnel_pid": tunnel_owner.get("pid") if tunnel else None,
            },
        )
        _write_lifecycle(home, enabled=True, restart_required=False)
        _append_log(home, f"started {entry.name} serve on 127.0.0.1:{runtime_port}")

        def _stop(_signum: int, _frame: Any) -> None:
            if child is not None and child.poll() is None:
                child.terminate()
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        while child.poll() is None:
            login.tick()
            if not startup_healthy.is_set():
                if _runtime_healthy(
                    home,
                    {"process": {"pid": child.pid, "port": runtime_port}},
                ):
                    with _settings_lock(home):
                        _persist_active_settings_locked(home, startup_settings)
                    with child_output_lock:
                        startup_healthy.set()
            time.sleep(0.2)
        _append_log(home, f"runtime entry exited {child.returncode}")
    except Exception as exc:
        startup_error = exc if isinstance(exc, RuntimeError_) else RuntimeError_(
            f"ChatGPT Web Runtime supervisor failed: {exc.__class__.__name__}"
        )
    finally:
        if login is not None:
            login.close()
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
        if server is not None:
            if thread is not None:
                server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if child_output_reader is not None:
            child_output_reader.join(timeout=2)
        if not startup_healthy.is_set() and (child is not None or startup_error is not None):
            with child_output_lock:
                output = child_output.decode("utf-8", "replace").strip()
            detail = "\n".join(
                part for part in (output, str(startup_error) if startup_error else "") if part
            )
            _write_startup_diagnostic(home, detail)
        if tunnel is not None:
            try:
                _stop_tunnel_runtime(home, tunnel)
            except Exception as exc:
                _append_log(home, f"owned Tunnel shutdown could not be confirmed: {exc}")
        (home / "process.json").unlink(missing_ok=True)
        lock_handle.close()
    if startup_error is not None:
        return _fail(home, str(startup_error))
    return 0 if child is not None and child.returncode == 0 else 1


def _try_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError from exc
        return
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise BlockingIOError from exc


def _running_status(home: Path) -> dict[str, Any] | None:
    record = _process_record(home)
    if record is None:
        return None
    port = record.get("diagnostic_port")
    if not isinstance(port, int):
        return None
    try:
        with urllib.request.urlopen(f"http://{LOOPBACK_HOST}:{port}/status", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    process = payload.get("process")
    if not isinstance(process, dict) or process.get("pid") != record.get("pid"):
        return None
    if process.get("private_home") != str(home):
        return None
    return payload


def _runtime_healthy(home: Path, status: dict[str, Any]) -> bool:
    process = status.get("process")
    config = _read_json(_web_home(home) / "config.json") or {}
    pid = process.get("pid") if isinstance(process, dict) else None
    port = process.get("port") if isinstance(process, dict) else None
    mode = config.get("mode")
    if (
        not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1
        or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
        or mode not in {"browser-only", "full"}
        or config.get("releaseVersion") != PINNED_VERSION
    ):
        return False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://{LOOPBACK_HOST}:{port}/healthz", timeout=RUNTIME_HEALTH_TIMEOUT_SECONDS
        ) as response:
            if response.status != 200:
                return False
            health = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(health, dict) or not (
        health.get("service") == "codex-chatgpt-web"
        and health.get("status") == "ok"
        and health.get("version") == config["releaseVersion"]
        and health.get("mode") == mode
        and health.get("pid") == pid
        and health.get("port") == port
        and health.get("accepting_turns") is True
    ):
        return False
    current = _process_record(home)
    return current is not None and current.get("pid") == pid


def _startup_log_detail(home: Path) -> str:
    try:
        detail = (home / "startup-diagnostic.log").read_bytes()[-MAX_STARTUP_DIAGNOSTIC_BYTES:]
    except OSError:
        return ""
    return redact(detail.decode("utf-8", "replace"), _secrets(home)).replace("\n", " ")[-2000:]


def _write_startup_diagnostic(home: Path, detail: str) -> None:
    safe = redact(detail, _secrets(home)).strip()
    if safe:
        payload = safe.encode("utf-8")[-MAX_STARTUP_DIAGNOSTIC_BYTES:]
        try:
            _write_private_bytes(home / "startup-diagnostic.log", payload)
        except OSError:
            pass


def _startup_failure_message(home: Path, supervisor_exit: int | None) -> str:
    status = "ChatGPT Web Runtime did not become healthy"
    if supervisor_exit is not None:
        status += f" (supervisor exited {supervisor_exit})"
    detail = _startup_log_detail(home)
    return f"{status}: {detail}" if detail else status


def start_runtime(home: Path) -> dict[str, Any]:
    home = _assert_private_home(home)
    _mkdir(home)
    _restore_install(home)
    pin = load_pin()
    if not _pin_compatible(home, pin):
        raise RuntimeError_("version mismatch; refusing to start an incompatible ChatGPT Web Runtime pin")
    _bind_host()
    _find_entry(_runtime_root(home))
    existing = _running_status(home)
    if existing is not None:
        if (
            not existing.get("restart_required")
            and existing.get("login", {}).get("control") == LOGIN_CONTROL
            and _runtime_healthy(home, existing)
        ):
            return existing
        stop_runtime(home, disable=False)
    if (_read_json(home / "lifecycle.json") or {}).get("enabled") is False:
        _write_lifecycle(home, enabled=True, restart_required=False)
    env = os.environ.copy()
    env["CODEXHUB_CHATGPT_WEB_HOME"] = str(home)
    env.pop("CODEX_WEB_GPT_DEV_HOME", None)
    (home / "startup-diagnostic.log").unlink(missing_ok=True)
    (home / "runtime-entry.log").unlink(missing_ok=True)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "supervise", "--home", str(home)],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def _startup_snapshot_matches(status: dict[str, Any]) -> bool:
        process_info = status.get("process")
        record = _process_record(home)
        if (
            not isinstance(process_info, dict)
            or record is None
            or record.get("supervisor_pid") != process.pid
            or record.get("pid") != process_info.get("pid")
        ):
            return False
        with _settings_lock(home):
            return _active_settings_match_config_locked(home)

    deadline = time.monotonic() + RUNTIME_STARTUP_TIMEOUT_SECONDS
    health_deadline: float | None = None
    try:
        while time.monotonic() < deadline:
            existing = _running_status(home)
            if existing is not None and _runtime_healthy(home, existing) and _startup_snapshot_matches(existing):
                return existing
            if existing is not None and health_deadline is None:
                health_deadline = time.monotonic() + RUNTIME_STARTUP_HEALTH_TIMEOUT_SECONDS
            if process.poll() is not None:
                break
            if health_deadline is not None and time.monotonic() >= health_deadline:
                break
            time.sleep(0.05)
        existing = _running_status(home)
        if existing is not None and _runtime_healthy(home, existing) and _startup_snapshot_matches(existing):
            return existing
        raise RuntimeError_("ChatGPT Web Runtime did not become healthy")
    except Exception as exc:
        if process.poll() is None:
            record = _read_json(home / "process.json") or {}
            if record.get("supervisor_pid") == process.pid:
                try:
                    _signal_supervisor(home)
                    process.wait(timeout=25)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=2)
            elif _process_record(home) is None:
                process.kill()
                process.wait(timeout=2)
        if isinstance(exc, RuntimeError_) and str(exc) == "ChatGPT Web Runtime did not become healthy":
            raise RuntimeError_(_startup_failure_message(home, process.poll())) from exc
        raise


def _signal_supervisor(home: Path) -> None:
    record = _read_json(home / "process.json") or {}
    try:
        supervisor_pid = int(record.get("supervisor_pid") or 0)
    except (TypeError, ValueError):
        supervisor_pid = 0
    if supervisor_pid and _pid_alive(supervisor_pid):
        command = _cmdline(supervisor_pid)
        if os.name == "nt" or (
            "chatgpt_web_runtime.py" in command and "supervise" in command and str(home) in command
        ):
            os.kill(supervisor_pid, signal.SIGTERM)
            return
    runtime = _process_record(home)
    if runtime is None:
        return
    os.kill(int(runtime["pid"]), signal.SIGTERM)


def stop_runtime(home: Path, *, disable: bool) -> dict[str, Any]:
    home = _assert_private_home(home)
    _mkdir(home)
    record = _read_json(home / "process.json") or {}
    try:
        runtime_pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        runtime_pid = 0
    try:
        supervisor_pid = int(record.get("supervisor_pid") or 0)
    except (TypeError, ValueError):
        supervisor_pid = 0
    _signal_supervisor(home)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and (
        (runtime_pid and _pid_alive(runtime_pid))
        or (supervisor_pid and _pid_alive(supervisor_pid))
    ):
        time.sleep(0.05)
    if runtime_pid and _pid_alive(runtime_pid):
        environ = set(_environ(runtime_pid).split("\n"))
        if f"CODEX_CHATGPT_WEB_HOME={_web_home(home)}" in environ:
            os.kill(runtime_pid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 2
        while time.monotonic() < kill_deadline and _pid_alive(runtime_pid):
            time.sleep(0.05)
    if supervisor_pid and _pid_alive(supervisor_pid):
        raise RuntimeError_("ChatGPT Web supervisor has not completed owned shutdown")
    if runtime_pid and _pid_alive(runtime_pid):
        raise RuntimeError_("ChatGPT Web Runtime process did not stop")
    if _tunnel_ownership_path(home).exists():
        try:
            _stop_tunnel_runtime(home, _tunnel_config(home))
        except RuntimeError_:
            raise
        except Exception as exc:
            raise RuntimeError_("ChatGPT Web Tunnel shutdown could not be confirmed") from exc
    (home / "process.json").unlink(missing_ok=True)
    state = _lifecycle(home)
    # Disable stops the supervisor only. Account files stay until delete-account.
    _write_lifecycle(
        home,
        enabled=not disable,
        restart_required=not disable,
        admitting=False if disable else state["admitting"],
    )
    _append_log(home, "disabled runtime" if disable else "stopped runtime; restart required")
    return build_status(home)


def open_login(home: Path) -> dict[str, Any]:
    home = _assert_private_home(home)
    if _read_json(home / "lifecycle.json") is not None and not _lifecycle(home)["enabled"]:
        raise RuntimeError_("ChatGPT Web Runtime is disabled")
    status = _running_status(home)
    if status is None:
        status = start_runtime(home)
    if status.get("login", {}).get("control") != LOGIN_CONTROL or status.get("restart_required"):
        raise RuntimeError_("Restart the ChatGPT Web component before opening its login window")
    return _login_command(home, "open")


def _login_command(home: Path, action: str) -> dict[str, Any]:
    # Serialize callers across app windows and CLI processes, including the
    # acknowledgement: the supervisor consumes one private mailbox at a time.
    deadline = time.monotonic() + 20
    with (home / "login-command.lock").open("a+") as lock:
        while True:
            try:
                _try_lock(lock)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError_("Another ChatGPT login command is still running")
                time.sleep(0.05)
        request_id = secrets.token_hex(16)
        _write_json(home / "login-command.json", {"action": action, "request_id": request_id}, mode=0o600)
        while time.monotonic() < deadline:
            window = _read_json(home / "window.json") or {}
            if window.get("request_id") == request_id:
                if window.get("error"):
                    raise RuntimeError_("ChatGPT browser login could not start; check the local browser installation")
                break
            time.sleep(0.05)
        else:
            raise RuntimeError_("ChatGPT login supervisor did not respond")
    return build_status(home)


def close_login(home: Path) -> dict[str, Any]:
    home = _assert_private_home(home)
    record = _process_record(home)
    if record is None or record.get("login_control") != LOGIN_CONTROL:
        return build_status(home)
    return _login_command(home, "close")


def _parse(argv: list[str]) -> tuple[list[str], Path, Path | None]:
    home: Path | None = None
    source: Path | None = None
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--home":
            if index + 1 >= len(argv):
                raise RuntimeError_("--home requires a path")
            home = Path(argv[index + 1])
            index += 2
            continue
        if item == "--source":
            if index + 1 >= len(argv):
                raise RuntimeError_("--source requires a path")
            source = Path(argv[index + 1])
            index += 2
            continue
        rest.append(item)
        index += 1
    return rest, home or default_home(), source


def main(argv: list[str] | None = None) -> int:
    try:
        rest, home, source = _parse(list(sys.argv[1:] if argv is None else argv))
    except RuntimeError_ as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}) + "\n")
        return 1
    command = rest[0] if rest else "status"
    if len(rest) > 1:
        return _fail(home, "unexpected ChatGPT Web Runtime arguments")
    try:
        if command == "install":
            _emit(home, install_runtime(home, source))
        elif command == "start":
            _emit(home, start_runtime(home))
        elif command == "stop":
            _emit(home, stop_runtime(home, disable=False))
        elif command == "disable":
            _emit(home, stop_runtime(home, disable=True))
        elif command == "upgrade":
            _emit(home, upgrade_runtime(home, source))
        elif command == "delete-account":
            _emit(home, delete_account(home))
        elif command == "status":
            home = _assert_private_home(home)
            _emit(home, build_status(home))
        elif command == "settings-get":
            _emit(home, read_settings(home))
        elif command == "settings-save":
            try:
                request = json.loads(sys.stdin.read())
            except json.JSONDecodeError as exc:
                raise RuntimeError_("ChatGPT Web settings request must be valid JSON") from exc
            _emit(home, save_settings(home, request))
        elif command == "open-login":
            _emit(home, open_login(home))
        elif command == "close-login":
            _emit(home, close_login(home))
        elif command == "supervise":
            return supervise(home)
        else:
            return _fail(home, f"unknown ChatGPT Web Runtime command: {command}")
    except RuntimeError_ as exc:
        return _fail(home, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(home, f"ChatGPT Web Runtime failed: {exc.__class__.__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
