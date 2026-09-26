#!/usr/bin/env python3
"""CodexHub supervisor for the pinned ChatGPT Web Runtime.

Upstream ``setup`` always installs Codex integration, including with
``--replace-codex-route``. The DEV harness refuses to start the Responses
listener. This process is the product entry: it verifies a pinned archive
into a private directory and does not execute that archive.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PINNED_COMMIT = "a13cd09950969f43e3b7e25c71fa43efaf5446c5"
PINNED_VERSION = "6.1.1"
LOOPBACK_HOST = "127.0.0.1"
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
ACCOUNT_BODY_LIMIT = 4096
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
REJECTED_ENTRIES = ("setup", "dev", "--replace-codex-route")
SECRET_ENV_NAMES = ("CODEX_WEB_GPT_DEV_HOME",)


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
    text = json.dumps(payload, sort_keys=True)
    temporary.write_text(text + "\n", encoding="utf-8")
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


def _secrets(home: Path) -> list[str]:
    account = _read_json(home / "account.json") or {}
    secret = account.get("secret")
    if isinstance(secret, str) and secret:
        return [secret]
    return []


def redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if len(secret) >= 4:
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


def _append_log(home: Path, message: str) -> None:
    _mkdir(home)
    line = redact(message, _secrets(home)).replace("\n", " ")
    with (home / "supervisor.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _assert_private_home(home: Path) -> Path:
    resolved = home.expanduser().resolve()
    forbidden: list[Path] = []
    for name in ("CODEX_HOME", *SECRET_ENV_NAMES):
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
    payload["_pin_path"] = str(pin_path)
    return payload


def _artifact(pin: dict[str, Any]) -> dict[str, Any]:
    artifact = pin["artifacts"][artifact_key()]
    if not isinstance(artifact, dict):
        raise RuntimeError_("ChatGPT Web Runtime pin artifact is invalid")
    return artifact


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
        and installed.get("executed") is False
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


def _layers(home: Path) -> dict[str, Any]:
    payload = _read_json(home / "layers.json") or {}
    browser = payload.get("browser_smoke")
    tunnel = payload.get("tunnel")
    return {
        "browser_smoke": browser if browser in {"not_run", "passed", "failed"} else "not_run",
        "tunnel": tunnel if tunnel in {"not_started", "ready", "failed"} else "not_started",
        "connector_selectable": payload.get("connector_selectable") is True,
        "detail": payload.get("detail") if isinstance(payload.get("detail"), str) else "",
    }


def _account_public(home: Path) -> dict[str, Any]:
    payload = _read_json(home / "account.json") or {}
    account_id = payload.get("account_id")
    signed_in = isinstance(account_id, str) and bool(account_id) and isinstance(payload.get("secret"), str)
    window = _read_json(home / "window.json") or {}
    return {
        "state": "signed_in" if signed_in else "signed_out",
        "account_id": account_id if signed_in else None,
        "window": "open" if window.get("open") is True else "closed",
    }


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


def _is_our_process(pid: int, home: Path) -> bool:
    if not _pid_alive(pid):
        return False
    if os.name == "nt":
        return True
    command = _cmdline(pid)
    return (
        "chatgpt_web_runtime.py" in command
        and "supervise" in command
        and str(home) in command
    )


def _process_record(home: Path) -> dict[str, Any] | None:
    payload = _read_json(home / "process.json")
    if not payload:
        return None
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if not _is_our_process(pid, home):
        return None
    return payload


def _supervisor_executable() -> str:
    return f"{sys.executable} {Path(__file__).resolve()}"


def build_status(home: Path, pin: dict[str, Any] | None = None) -> dict[str, Any]:
    loaded = pin or load_pin()
    artifact = _artifact(loaded)
    installed = _install_document(home)
    compatible = _pin_compatible(home, loaded)
    layers = _layers(home)
    login = _account_public(home)
    lifecycle = _lifecycle(home)
    process = _process_record(home)
    running = process is not None
    ready = bool(
        compatible
        and running
        and lifecycle["enabled"]
        and not lifecycle["restart_required"]
        and login["state"] == "signed_in"
        and layers["browser_smoke"] == "passed"
        and layers["tunnel"] == "ready"
        and layers["connector_selectable"] is True
    )
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
        "login": {"state": login["state"], "window": login["window"], "account_id": login["account_id"]},
        "browser_smoke": {"state": layers["browser_smoke"]},
        "tunnel": {"state": layers["tunnel"], "detail": layers["detail"]},
        "connector": {"selectable": layers["connector_selectable"]},
        "process": {
            "pid": None if process is None else process.get("pid"),
            "port": None if process is None else process.get("port"),
            "executable": _supervisor_executable(),
            "private_home": str(home),
            "running": running,
            "ownership": "codexhub-supervisor",
            "listen_host": LOOPBACK_HOST if running else None,
        },
        "ready": ready,
        "restart_required": lifecycle["restart_required"],
        "disabled": not lifecycle["enabled"],
        "installed": installed is not None,
        "entry": "codexhub-supervisor",
        "upstream_executed": False,
        "rejected_entries": list(REJECTED_ENTRIES),
    }


def _emit(home: Path, payload: dict[str, Any]) -> None:
    text = redact(json.dumps(payload, sort_keys=True), _secrets(home))
    sys.stdout.write(text + "\n")


def _fail(home: Path, message: str) -> int:
    safe = redact(message, _secrets(home))
    _append_log(home, safe)
    sys.stdout.write(json.dumps({"ok": False, "error": safe}, sort_keys=True) + "\n")
    return 1


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


def install_runtime(home: Path, source: Path | None = None) -> dict[str, Any]:
    home = _assert_private_home(home)
    _mkdir(home)
    _restore_install(home)
    pin = load_pin()
    artifact = _artifact(pin)
    partial = home / "staging" / "payload.partial"
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
        incoming = home / "incoming"
        if incoming.exists():
            shutil.rmtree(incoming)
        _mkdir(incoming)
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
                "executed": False,
                "entry": "codexhub-supervisor",
            },
        )
        _promote(home, incoming)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    _append_log(home, "installed pinned runtime without executing the archive")
    _write_lifecycle(home, enabled=True, restart_required=False)
    return build_status(home, pin)


def _bind_host() -> str:
    requested = os.environ.get("CODEXHUB_CHATGPT_WEB_BIND", "").strip() or LOOPBACK_HOST
    if requested != LOOPBACK_HOST:
        raise RuntimeError_("ChatGPT Web Runtime status bind must be 127.0.0.1")
    return requested


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

    def _status_bytes(self) -> bytes:
        home: Path = self.server.home  # type: ignore[attr-defined]
        text = redact(json.dumps(build_status(home), sort_keys=True), _secrets(home))
        return text.encode("utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if not self._client_allowed():
            self._send(403, b'{"ok":false,"error":"loopback only"}', "application/json")
            return
        if self.path.split("?", 1)[0] == "/status":
            self._send(200, self._status_bytes(), "application/json")
            return
        if self.path.split("?", 1)[0] == "/login":
            page = (
                "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>ChatGPT Web Runtime</title></head>"
                "<body><h1>ChatGPT Web Runtime</h1>"
                "<p>Private account form for the single local runtime account. "
                "This page does not sign in to chatgpt.com and does not run browser smoke.</p>"
                "<form method=\"POST\" action=\"/account\">"
                "<label>Account secret <input name=\"secret\" type=\"password\" autocomplete=\"off\"></label>"
                "<button type=\"submit\">Save account</button></form></body></html>"
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        self._send(404, b'{"ok":false,"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        if not self._client_allowed():
            self._send(403, b'{"ok":false,"error":"loopback only"}', "application/json")
            return
        if self.path.split("?", 1)[0] != "/account":
            self._send(404, b'{"ok":false,"error":"not found"}', "application/json")
            return
        home: Path = self.server.home  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = ACCOUNT_BODY_LIMIT + 1
        if length < 0 or length > ACCOUNT_BODY_LIMIT:
            self._send(413, b'{"ok":false,"error":"account form rejected"}', "application/json")
            return
        raw = self.rfile.read(length)
        secret = _secret_from_body(raw, self.headers.get("Content-Type", ""))
        if secret is None:
            _append_log(home, "rejected account form")
            self._send(400, b'{"ok":false,"error":"account form rejected"}', "application/json")
            return
        try:
            _store_account(home, secret)
        except RuntimeError_ as exc:
            message = redact(str(exc), [secret, *_secrets(home)])
            _append_log(home, message)
            body = json.dumps({"ok": False, "error": message}).encode("utf-8")
            self._send(409, body, "application/json")
            return
        _append_log(home, "stored one local account")
        self._send(200, self._status_bytes(), "application/json")


def _secret_from_body(raw: bytes, content_type: str) -> str | None:
    text = raw.decode("utf-8", "replace")
    if "application/json" in content_type:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        secret = payload.get("secret")
    else:
        parsed = urllib.parse.parse_qs(text, keep_blank_values=False)
        values = parsed.get("secret") or []
        secret = values[0] if values else None
    if not isinstance(secret, str) or len(secret) < 8 or len(secret) > 512:
        return None
    return secret


def _store_account(home: Path, secret: str) -> None:
    existing = _read_json(home / "account.json")
    if existing and isinstance(existing.get("secret"), str):
        stored = str(existing["secret"])
        same = len(stored) == len(secret) and hmac.compare_digest(stored, secret)
        if not same:
            raise RuntimeError_("single account already initialized")
        return
    _write_json(
        home / "account.json",
        {"account_id": str(uuid.uuid4()), "secret": secret},
        mode=0o600,
    )


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


def supervise(home: Path) -> int:
    home = _assert_private_home(home)
    _mkdir(home)
    pin = load_pin()
    if not _pin_compatible(home, pin):
        return _fail(home, "version mismatch; refusing to start an incompatible ChatGPT Web Runtime pin")
    if not _lifecycle(home)["enabled"]:
        return _fail(home, "ChatGPT Web Runtime is disabled")
    host = _bind_host()
    lock_path = home / "supervisor.lock"
    lock_handle = lock_path.open("a+")
    try:
        _try_lock(lock_handle)
    except BlockingIOError:
        lock_handle.close()
        return _fail(home, "ChatGPT Web Runtime supervisor is already running")
    server = ThreadingHTTPServer((host, 0), _StatusHandler)
    bound_host, port = server.server_address[:2]
    if bound_host != LOOPBACK_HOST:
        server.server_close()
        lock_handle.close()
        return _fail(home, "refusing to listen outside 127.0.0.1")
    server.home = home  # type: ignore[attr-defined]
    _write_json(
        home / "process.json",
        {
            "pid": os.getpid(),
            "port": port,
            "executable": _supervisor_executable(),
            "private_home": str(home),
            "ownership": "codexhub-supervisor",
        },
    )
    _write_lifecycle(home, enabled=True, restart_required=False)
    _append_log(home, f"supervisor listening on 127.0.0.1:{port}")

    def _stop_server(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop_server)
    signal.signal(signal.SIGINT, _stop_server)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        process = home / "process.json"
        process.unlink(missing_ok=True)
        lock_handle.close()
        _append_log(home, "supervisor stopped")
    return 0


def _running_status(home: Path) -> dict[str, Any] | None:
    record = _process_record(home)
    if record is None:
        return None
    port = record.get("port")
    if not isinstance(port, int):
        return None
    try:
        with urllib.request.urlopen(f"http://{LOOPBACK_HOST}:{port}/status", timeout=1) as response:
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
    existing = _running_status(home)
    if existing is not None:
        return existing
    if (_read_json(home / "lifecycle.json") or {}).get("enabled") is False:
        _write_lifecycle(home, enabled=True, restart_required=False)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "supervise",
        "--home",
        str(home),
    ]
    subprocess_env = os.environ.copy()
    subprocess_env["CODEXHUB_CHATGPT_WEB_HOME"] = str(home)
    subprocess_env.pop("CODEX_WEB_GPT_DEV_HOME", None)
    process = subprocess.Popen(
        command,
        env=subprocess_env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
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
        raise RuntimeError_("ChatGPT Web Runtime supervisor did not become ready")
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        raise


def _signal_supervisor(home: Path) -> dict[str, Any] | None:
    record = _process_record(home)
    if record is None:
        return None
    pid = int(record["pid"])
    if not _is_our_process(pid, home):
        raise RuntimeError_("refusing to signal a process CodexHub does not own")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            return record
        time.sleep(0.05)
    if _is_our_process(pid, home):
        os.kill(pid, signal.SIGKILL)
    return record


def stop_runtime(home: Path, *, disable: bool) -> dict[str, Any]:
    home = _assert_private_home(home)
    _mkdir(home)
    _signal_supervisor(home)
    _write_lifecycle(home, enabled=not disable, restart_required=not disable)
    _append_log(home, "disabled runtime" if disable else "stopped runtime; restart required")
    return build_status(home)


def open_login(home: Path) -> dict[str, Any]:
    home = _assert_private_home(home)
    lifecycle = _lifecycle(home)
    if _read_json(home / "lifecycle.json") is not None and not lifecycle["enabled"]:
        raise RuntimeError_("ChatGPT Web Runtime is disabled")
    status = _running_status(home)
    if status is None:
        status = start_runtime(home)
    port = status.get("process", {}).get("port")
    if not isinstance(port, int):
        raise RuntimeError_("login window has no loopback port")
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
