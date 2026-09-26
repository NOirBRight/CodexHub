#!/usr/bin/env python3
"""CodexHub supervisor for the pinned ChatGPT Web Runtime.

Upstream ``setup`` always calls ``installCodexIntegration``. ``dev`` refuses
to start a Responses listener, and the upstream installer scripts are not
used. After the archive checksum matches, this module extracts it and starts
``bin/codex-chatgpt-web serve`` with config and homes kept inside the private
runtime directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

PINNED_COMMIT = "a13cd09950969f43e3b7e25c71fa43efaf5446c5"
PINNED_VERSION = "6.1.1"
LOOPBACK_HOST = "127.0.0.1"
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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    try:
        os.chmod(path, mode)
    except OSError:
        return


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
        if len(secret) >= 8:
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
    token = _control_token(home)
    return [token] if token else []


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
    url = str(artifact.get("url") or "")
    if len(sha) != 64 or any(character not in "0123456789abcdef" for character in sha):
        raise RuntimeError_("ChatGPT Web Runtime pin checksum is invalid")
    _assert_pinned_url(url, PINNED_VERSION)
    return payload


def _artifact(pin: dict[str, Any]) -> dict[str, Any]:
    artifact = pin["artifacts"][artifact_key()]
    if not isinstance(artifact, dict):
        raise RuntimeError_("ChatGPT Web Runtime pin artifact is invalid")
    return artifact


def _runtime_root(home: Path) -> Path:
    return home / "current" / "runtime"


def _web_home(home: Path) -> Path:
    return home / "web-home"


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
    }


def _write_lifecycle(home: Path, *, enabled: bool, restart_required: bool) -> None:
    _write_json(
        home / "lifecycle.json",
        {"enabled": enabled, "restart_required": restart_required},
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
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


def _copy_to_partial(source: Path, dest: Path) -> None:
    if not source.is_file():
        raise RuntimeError_("runtime source is not a file")
    _mkdir(dest.parent)
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


def _download_to_partial(url: str, dest: Path, version: str) -> None:
    _assert_pinned_url(url, version)
    request = urllib.request.Request(url, headers={"User-Agent": "CodexHub"})
    _mkdir(dest.parent)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.hostname not in ALLOWED_DOWNLOAD_HOSTS:
                raise RuntimeError_("runtime download host is not pinned")
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


def install_runtime(home: Path, source: Path | None = None) -> dict[str, Any]:
    home = _assert_private_home(home)
    _mkdir(home)
    _restore_install(home)
    pin = load_pin()
    artifact = _artifact(pin)
    partial = home / "staging" / "payload.partial"
    incoming = home / "incoming"
    try:
        if source is None:
            _download_to_partial(str(artifact["url"]), partial, PINNED_VERSION)
        else:
            _copy_to_partial(source, partial)
        digest = _sha256(partial)
        expected = str(artifact["sha256"])
        if not hmac.compare_digest(digest, expected):
            partial.unlink(missing_ok=True)
            raise RuntimeError_("checksum mismatch; payload was not executed")
        os.chmod(partial, 0o644)
        if incoming.exists():
            shutil.rmtree(incoming)
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
                "archive_executed": False,
                "entry": str(entry.relative_to(runtime_dest)),
            },
        )
        _promote(home, incoming)
    except Exception:
        partial.unlink(missing_ok=True)
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise
    _append_log(home, "extracted pinned runtime; archive was not executed")
    _write_lifecycle(home, enabled=True, restart_required=False)
    return build_status(home, pin)


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


def _write_minimum_config(home: Path, entry: Path) -> int:
    web_home = _web_home(home)
    _mkdir(web_home / "browser")
    _mkdir(web_home / "socket")
    _mkdir(_codex_home(home))
    port = _free_port()
    token = secrets.token_urlsafe(48)
    _write_json(
        web_home / "config.json",
        {
            "version": 3,
            "releaseVersion": PINNED_VERSION,
            "mode": "browser-only",
            "subagentProtocol": "compatibility-v1",
            "host": LOOPBACK_HOST,
            "port": port,
            "contextWindow": 256000,
            "appName": CONNECTOR_NAME,
            "automaticAppName": CONNECTOR_NAME,
            "manualAppName": ZERO_RISK_CONNECTOR_NAME,
            "browserHost": "managed-chrome",
            "browserInteractionMode": "automatic",
            "chromeExecutablePath": "/usr/bin/google-chrome",
            "storageStatePath": str(web_home / "browser" / "storage-state.json"),
            "brokerSocketPath": str(web_home / "socket" / "turn-broker.sock"),
            "headed": True,
            "solAvailable": True,
            "extraHighAvailable": False,
            "proAvailable": False,
            "experimentalBiggerContext": False,
            "experimentalSkillAttachments": False,
            "experimentalFreshConversationPerTurn": False,
            "useSavedChats": False,
            "zeroRiskProEnabled": False,
            "autoApproveToolCalls": False,
            "controlToken": token,
            "runtimeCommand": [str(entry)],
        },
        mode=0o600,
    )
    return port


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
    stdout = redact(completed.stdout, _secrets(home))
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        _append_log(home, "runtime doctor did not return JSON")
        return None
    return payload if isinstance(payload, dict) else None


def _layers_from_doctor(report: dict[str, Any] | None) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    if report:
        for check in report.get("checks") or []:
            if isinstance(check, dict) and isinstance(check.get("id"), str):
                checks[str(check["id"])] = check
    login = checks.get("login")
    smoke = checks.get("browser-smoke")
    tunnel = checks.get("tunnel-runtime")
    connector = checks.get("connector")
    if smoke is None:
        browser = "not_run"
    elif smoke.get("status") == "ok":
        browser = "passed"
    else:
        browser = "failed"
    if tunnel is None:
        tunnel_state = "not_started"
    elif tunnel.get("status") == "ok":
        tunnel_state = "ready"
    else:
        tunnel_state = "failed"
    detail = tunnel.get("message") if isinstance(tunnel, dict) else ""
    if not isinstance(detail, str):
        detail = ""
    return {
        "login": "signed_in" if login and login.get("status") == "ok" else "signed_out",
        "browser_smoke": browser,
        "tunnel": tunnel_state,
        "tunnel_detail": detail,
        "connector_selectable": bool(connector and connector.get("status") == "ok"),
    }


def build_status(home: Path, pin: dict[str, Any] | None = None) -> dict[str, Any]:
    loaded = pin or load_pin()
    artifact = _artifact(loaded)
    installed = _install_document(home)
    compatible = _pin_compatible(home, loaded)
    record = _process_record(home)
    running = record is not None
    layers = _layers_from_doctor(None)
    if running and installed is not None:
        entry = _find_entry(_runtime_root(home))
        layers = _layers_from_doctor(_run_doctor(home, entry))
    lifecycle = _lifecycle(home)
    window = _read_json(home / "window.json") or {}
    ready = bool(
        compatible
        and running
        and lifecycle["enabled"]
        and not lifecycle["restart_required"]
        and layers["login"] == "signed_in"
        and layers["browser_smoke"] == "passed"
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
            "window": "open" if window.get("open") is True else "closed",
            "account_id": None,
        },
        "browser_smoke": {"state": layers["browser_smoke"]},
        "tunnel": {"state": layers["tunnel"], "detail": layers["tunnel_detail"]},
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
        "ready": ready,
        "restart_required": lifecycle["restart_required"],
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
        "<p>Login, browser smoke, tunnel, and connector stay unready until "
        "<code>codex-chatgpt-web doctor</code> reports them.</p></body></html>"
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
            text = redact(json.dumps(build_status(home), sort_keys=True), _secrets(home))
            self._send(200, text.encode("utf-8"), "application/json")
            return
        if path == "/login":
            self._send(200, _diagnostic_page(), "text/html; charset=utf-8")
            return
        self._send(404, b'{"ok":false,"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        self._send(404, b'{"ok":false,"error":"not found"}', "application/json")


def _emit(home: Path, payload: dict[str, Any]) -> None:
    sys.stdout.write(redact(json.dumps(payload, sort_keys=True), _secrets(home)) + "\n")


def _fail(home: Path, message: str) -> int:
    safe = redact(message, _secrets(home))
    try:
        _append_log(home, safe)
    except OSError:
        pass
    sys.stdout.write(json.dumps({"ok": False, "error": safe}, sort_keys=True) + "\n")
    return 1


def supervise(home: Path) -> int:
    home = _assert_private_home(home)
    _mkdir(home)
    pin = load_pin()
    if not _pin_compatible(home, pin):
        return _fail(home, "version mismatch; refusing to start an incompatible ChatGPT Web Runtime pin")
    if not _lifecycle(home)["enabled"]:
        return _fail(home, "ChatGPT Web Runtime is disabled")
    host = _bind_host()
    try:
        entry = _find_entry(_runtime_root(home))
    except RuntimeError_ as exc:
        return _fail(home, str(exc))
    runtime_port = _write_minimum_config(home, entry)
    lock_handle = (home / "supervisor.lock").open("a+")
    try:
        _try_lock(lock_handle)
    except BlockingIOError:
        lock_handle.close()
        return _fail(home, "ChatGPT Web Runtime supervisor is already running")
    entry_log = (home / "runtime-entry.log").open("ab")
    child = subprocess.Popen(
        [str(entry), "serve"],
        env=_runtime_env(home),
        cwd=str(_web_home(home)),
        stdin=subprocess.DEVNULL,
        stdout=entry_log,
        stderr=subprocess.STDOUT,
    )
    server = ThreadingHTTPServer((host, 0), _StatusHandler)
    bound_host, diagnostic_port = server.server_address[:2]
    if bound_host != LOOPBACK_HOST:
        child.terminate()
        server.server_close()
        lock_handle.close()
        entry_log.close()
        return _fail(home, "refusing to listen outside 127.0.0.1")
    server.home = home  # type: ignore[attr-defined]
    _write_json(
        home / "process.json",
        {
            "pid": child.pid,
            "supervisor_pid": os.getpid(),
            "port": runtime_port,
            "diagnostic_port": diagnostic_port,
            "executable": str(entry),
            "private_home": str(home),
            "ownership": "codexhub-supervisor",
        },
    )
    _write_lifecycle(home, enabled=True, restart_required=False)
    _append_log(home, f"started {entry.name} serve on 127.0.0.1:{runtime_port}")

    def _stop(_signum: int, _frame: Any) -> None:
        if child.poll() is None:
            child.terminate()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        while child.poll() is None:
            time.sleep(0.2)
        _append_log(home, f"runtime entry exited {child.returncode}")
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        entry_log.close()
        (home / "process.json").unlink(missing_ok=True)
        lock_handle.close()
    return 0 if child.returncode == 0 else 1


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
        return existing
    if (_read_json(home / "lifecycle.json") or {}).get("enabled") is False:
        _write_lifecycle(home, enabled=True, restart_required=False)
    env = os.environ.copy()
    env["CODEXHUB_CHATGPT_WEB_HOME"] = str(home)
    env.pop("CODEX_WEB_GPT_DEV_HOME", None)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "supervise", "--home", str(home)],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            existing = _running_status(home)
            if existing is not None:
                return existing
            if process.poll() is not None:
                break
            time.sleep(0.05)
        existing = _running_status(home)
        if existing is not None:
            return existing
        raise RuntimeError_("ChatGPT Web Runtime entry did not become ready")
    except Exception:
        if process.poll() is None and _process_record(home) is None:
            process.kill()
            process.wait(timeout=2)
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
    _signal_supervisor(home)
    deadline = time.time() + 5
    while time.time() < deadline and runtime_pid and _pid_alive(runtime_pid):
        time.sleep(0.05)
    if runtime_pid and _pid_alive(runtime_pid):
        environ = set(_environ(runtime_pid).split("\n"))
        if f"CODEX_CHATGPT_WEB_HOME={_web_home(home)}" in environ:
            os.kill(runtime_pid, signal.SIGKILL)
        kill_deadline = time.time() + 2
        while time.time() < kill_deadline and _pid_alive(runtime_pid):
            time.sleep(0.05)
    _write_lifecycle(home, enabled=not disable, restart_required=not disable)
    _append_log(home, "disabled runtime" if disable else "stopped runtime; restart required")
    return build_status(home)


def open_login(home: Path) -> dict[str, Any]:
    home = _assert_private_home(home)
    if _read_json(home / "lifecycle.json") is not None and not _lifecycle(home)["enabled"]:
        raise RuntimeError_("ChatGPT Web Runtime is disabled")
    status = _running_status(home)
    if status is None:
        status = start_runtime(home)
    port = status.get("process", {}).get("diagnostic_port")
    if not isinstance(port, int):
        raise RuntimeError_("login diagnostic page has no loopback port")
    _write_json(home / "window.json", {"open": True})
    status = build_status(home)
    status["login_url"] = f"http://{LOOPBACK_HOST}:{port}/login"
    return status


def close_login(home: Path) -> dict[str, Any]:
    home = _assert_private_home(home)
    _mkdir(home)
    _write_json(home / "window.json", {"open": False})
    return build_status(home)


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
        elif command == "status":
            home = _assert_private_home(home)
            _emit(home, build_status(home))
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
