"""Explicit, account-bound readiness checks for the managed ChatGPT Web runtime."""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import hashlib
import http.server
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import chatgpt_web_runtime as runtime

_CACHE_NAME = "readiness-checks.json"
_BROWSER_HELPER_NAME = "browser-helper.cjs"
_ACCOUNT_KEY_RE = re.compile(r"[0-9a-f]{64}")
_HELPER_OPERATION_TIMEOUT = 120
_SMOKE_OPERATION_TIMEOUT = 240
_IDLE_URL = (
    "data:text/html;charset=utf-8,%3C!doctype%20html%3E%3Chtml%3E%3Chead%3E"
    "%3Cmeta%20charset%3D%22utf-8%22%3E%3Ctitle%3ECodex%20Web%20GPT%3C%2Ftitle%3E"
    "%3C%2Fhead%3E%3Cbody%3E%3C%2Fbody%3E%3C%2Fhtml%3E#codex-web-gpt-browser-host"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _storage_state_path(home: Path, config: Mapping[str, Any]) -> Path:
    storage_path = Path(str(config.get("storageStatePath") or runtime._storage_state(home)))
    if not storage_path.is_absolute():
        storage_path = (runtime._web_home(home) / storage_path).resolve()
    return storage_path


def _account_identity_path(storage_path: Path) -> Path:
    return storage_path.with_name(storage_path.name + ".identity.json")


def _attested_account_key(storage_path: Path, account_state: bytes) -> str | None:
    attestation = runtime._read_json(_account_identity_path(storage_path))
    if not isinstance(attestation, dict):
        return None
    account_key = attestation.get("accountKey")
    state_digest = attestation.get("storageStateSha256")
    version = attestation.get("version")
    if (
        version != 1
        or isinstance(version, bool)
        or not isinstance(account_key, str)
        or _ACCOUNT_KEY_RE.fullmatch(account_key) is None
        or state_digest != _sha256(account_state)
    ):
        return None
    return account_key


def _binding(home: Path) -> dict[str, str] | None:
    """Fingerprint the loaded config and account files without returning either."""
    config_path = runtime._web_home(home) / "config.json"
    try:
        raw_config = config_path.read_bytes()
        config = json.loads(raw_config)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    storage_path = _storage_state_path(home, config)
    verified_path = storage_path.with_name(storage_path.name + ".verified.json")
    try:
        account = storage_path.read_bytes()
        verified = verified_path.read_bytes()
    except OSError:
        return None
    process = runtime._process_record(home)
    if process is None:
        return None
    process_identity = {
        key: process.get(key)
        for key in ("pid", "supervisor_pid", "port", "diagnostic_port", "executable", "ownership")
    }
    account_key = _attested_account_key(storage_path, account)
    account_binding = (
        b"identity:" + account_key.encode("ascii")
        if account_key is not None
        else account
    )
    return {
        "active_config_sha256": _sha256(raw_config),
        "account_state_sha256": _sha256(account_binding + b"\0" + verified),
        "runtime_instance_sha256": _sha256(json.dumps(process_identity, sort_keys=True).encode("utf-8")),
    }


def _write_account_identity_attestation(
    storage_path: Path, account_key: str, state_digest: str,
) -> bool:
    if _ACCOUNT_KEY_RE.fullmatch(account_key) is None:
        return False
    try:
        runtime._write_json(
            _account_identity_path(storage_path),
            {"version": 1, "accountKey": account_key, "storageStateSha256": state_digest},
            mode=0o600,
        )
    except OSError:
        return False
    return True


def _empty_result(*, cache_state: str, reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "state": "unchecked",
        "cache_state": cache_state,
        "checked_at": None,
        "browser": {"state": "not_checked"},
        "login": {"state": "not_checked", "capabilities": {}},
        "connector": {"state": "not_checked", "name": None},
        "models": [],
        "model_state": "not_checked",
        "runtime_capabilities": {},
        "capabilities_match": None,
        "text_ready": False,
        "tools_ready": False,
        "tunnel": {"state": "not_checked"},
        "reason": reason,
    }


def cached_checks(home: Path) -> dict[str, Any]:
    """Return saved evidence only when it still belongs to the active account/config."""
    home = Path(home).expanduser().resolve()
    current = _binding(home)
    path = home / _CACHE_NAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        document = None
    if not isinstance(document, dict):
        return _empty_result(cache_state="missing", reason="checks_not_run")
    saved_binding = document.get("binding")
    if current is None or not isinstance(saved_binding, dict):
        return _empty_result(cache_state="stale", reason="active_config_or_account_changed")
    if saved_binding != current:
        reason = (
            "runtime_instance_changed"
            if saved_binding.get("active_config_sha256") == current.get("active_config_sha256")
            and saved_binding.get("account_state_sha256") == current.get("account_state_sha256")
            else "active_config_or_account_changed"
        )
        return _empty_result(cache_state="stale", reason=reason)
    result = document.get("result")
    if not isinstance(result, dict):
        return _empty_result(cache_state="stale", reason="cached_evidence_invalid")
    try:
        datetime.fromisoformat(str(result.get("checked_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _empty_result(cache_state="stale", reason="cached_evidence_invalid")
    return {**result, "cache_state": "current"}


def _save_result(home: Path, binding: dict[str, str], result: dict[str, Any]) -> None:
    runtime._write_json(
        home / _CACHE_NAME,
        {"version": 1, "binding": binding, "result": result},
        mode=0o600,
    )


def _request_json(url: str, token: str, *, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("runtime_check_unavailable") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("runtime_check_invalid_response")
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _active_turn_counts(payload: dict[str, Any]) -> tuple[int, int] | None:
    http_turns = payload.get("active_http_turns")
    browser_turns = payload.get("active_browser_turns")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in (http_turns, browser_turns)):
        return None
    return int(http_turns), int(browser_turns)


def normalize_control_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the versioned, authenticated runtime status contract."""
    version = payload.get("control_contract_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("runtime_control_contract_invalid")
    if version != 1 or payload.get("status") != "ok":
        raise ValueError("runtime_control_contract_unsupported")
    if not isinstance(payload.get("accepting_turns"), bool):
        raise ValueError("runtime_control_contract_invalid")
    capabilities = payload.get("account_capabilities")
    if not isinstance(capabilities, Mapping):
        raise ValueError("runtime_control_contract_invalid")
    capability_fields = {
        "solAvailable": "sol_available",
        "extraHighAvailable": "extra_high_available",
        "proAvailable": "pro_available",
    }
    normalized_capabilities: dict[str, bool] = {}
    for public_name, source_name in capability_fields.items():
        value = capabilities.get(source_name)
        if not isinstance(value, bool):
            raise ValueError("runtime_control_contract_invalid")
        normalized_capabilities[public_name] = value
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ValueError("runtime_control_contract_invalid")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in raw_models:
        if not isinstance(value, Mapping):
            raise ValueError("runtime_control_contract_invalid")
        model_id = value.get("id")
        display_name = value.get("display_name")
        efforts = value.get("efforts")
        image_input = value.get("image_input")
        if (
            not isinstance(model_id, str)
            or not model_id.startswith("chatgpt-web/")
            or not isinstance(display_name, str)
            or not isinstance(efforts, list)
            or any(not isinstance(effort, str) or not effort.strip() for effort in efforts)
            or not isinstance(image_input, bool)
        ):
            raise ValueError("runtime_control_contract_invalid")
        model = runtime.normalize_runtime_model(value)
        if model is None or model["id"] in seen:
            raise ValueError("runtime_control_contract_invalid")
        seen.add(model["id"])
        models.append(model)
    counts = _active_turn_counts(payload)
    if counts is None:
        raise ValueError("runtime_control_contract_invalid")
    return {
        "accepting_turns": payload["accepting_turns"],
        "active_turns": {"http": counts[0], "browser": counts[1]},
        "account_capabilities": normalized_capabilities,
        "models": models,
    }


def read_control_status(port: int, token: str) -> dict[str, Any]:
    """Fetch the protected v1 control contract from the current loopback instance."""
    payload = _request_json(f"http://127.0.0.1:{port}/admin/status", token)
    try:
        return normalize_control_status(payload)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


class _ControlHandler(http.server.BaseHTTPRequestHandler):
    server_version = "CodexHubReadinessControl/1"

    def log_message(self, _fmt: str, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        server = self.server
        token = getattr(server, "token", "")
        if self.client_address[0] != "127.0.0.1" or self.headers.get("Authorization") != f"Bearer {token}":
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 16_384:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return
        trace_id = body.get("traceId") if isinstance(body, dict) else None
        if not isinstance(trace_id, str) or not trace_id.startswith("smoke_"):
            self.send_error(400)
            return
        if self.path == "/v1/turn/start":
            if getattr(server, "leased", False):
                self.send_error(409)
                return
            server.leased = True
            response = {"ok": True, "surfaceId": server.surface_id, "reused": False,
                        "connectorBound": False, "trackUsage": False}
        elif self.path == "/v1/turn/heartbeat":
            response = {"ok": True}
        elif self.path == "/v1/turn/end":
            server.leased = False
            response = {"ok": True, "cancelledByUser": False}
        else:
            self.send_error(404)
            return
        data = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _PinnedBrowserSession(AbstractContextManager["_PinnedBrowserSession"]):
    """Temporary CDP context with the active account state and no shared runtime tab."""

    def __init__(self, home: Path, config: dict[str, Any]):
        self.home = home
        self.config = config
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.browser: subprocess.Popen[bytes] | None = None
        self.bootstrap: subprocess.Popen[str] | None = None
        self.bootstrap_lines: _OutputLines | None = None
        self.control: http.server.ThreadingHTTPServer | None = None
        self.control_thread: threading.Thread | None = None
        self.descriptor_path: Path | None = None
        self.helper: Path | None = None
        self.bun: Path | None = None
        self.surface_id = secrets.token_hex(16)

    def __enter__(self) -> "_PinnedBrowserSession":
        try:
            self._open()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _open(self) -> None:
        root = runtime._runtime_root(self.home)
        self.helper = next((path for path in root.rglob(_BROWSER_HELPER_NAME) if path.is_file()), None)
        if self.helper is None:
            raise RuntimeError("pinned_browser_helper_missing")
        self.bun = _find_bun(root)
        playwright = next((path for path in root.rglob("playwright-core")
                           if path.is_dir() and (path / "package.json").is_file()), None)
        if self.bun is None or playwright is None:
            raise RuntimeError("pinned_browser_support_missing")
        executable = Path(str(self.config.get("chromeExecutablePath") or runtime._browser_executable()))
        if not executable.is_file():
            raise RuntimeError("managed_browser_missing")
        storage = Path(str(self.config.get("storageStatePath") or runtime._storage_state(self.home)))
        if not storage.is_absolute():
            storage = (runtime._web_home(self.home) / storage).resolve()
        if not storage.is_file():
            raise RuntimeError("managed_account_state_missing")

        self.temporary = tempfile.TemporaryDirectory(prefix="codexhub-web-check-")
        temporary = Path(self.temporary.name)
        profile = temporary / "chrome-profile"
        profile.mkdir(mode=0o700)
        self.browser = subprocess.Popen(
            [str(executable), *(["--headless=new"] if self.config.get("headed") is not True else []),
             "--disable-gpu", "--disable-dev-shm-usage",
             "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             *(["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []), "about:blank"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        endpoint = _wait_for_cdp(profile / "DevToolsActivePort", self.browser)
        self.control = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ControlHandler)
        self.control.token = secrets.token_urlsafe(36)  # type: ignore[attr-defined]
        self.control.surface_id = self.surface_id  # type: ignore[attr-defined]
        self.control.leased = False  # type: ignore[attr-defined]
        self.control_thread = threading.Thread(target=self.control.serve_forever, daemon=True)
        self.control_thread.start()
        control_port = int(self.control.server_address[1])
        bootstrap_path = temporary / "browser-context.cjs"
        bootstrap_path.write_text(_bootstrap_source(), encoding="utf-8")
        self.bootstrap = subprocess.Popen(
            [str(self.bun), str(bootstrap_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={
                **os.environ,
                "CODEXHUB_CHECK_CDP": endpoint,
                "CODEXHUB_CHECK_STATE": str(storage),
                "CODEXHUB_CHECK_PLAYWRIGHT": str(playwright),
            },
        )
        assert self.bootstrap.stdout is not None
        self.bootstrap_lines = _OutputLines(self.bootstrap.stdout)
        ready = _read_line(self.bootstrap, self.bootstrap_lines, 30)
        if ready.get("ok") is not True or not isinstance(ready.get("target_id"), str):
            raise RuntimeError("managed_browser_context_failed")
        control_token = self.control.token  # type: ignore[attr-defined]
        descriptor = {
            "version": 3,
            "kind": "codex-web-gpt-launcher",
            "profile": "production",
            "pid": self.browser.pid,
            "endpoint": endpoint,
            "control": {"endpoint": f"http://127.0.0.1:{control_port}", "token": control_token},
            "helper": {"executable": str(self.bun), "script": str(self.helper)},
            "partition": "persist:codex-web-gpt-chatgpt",
            "idleUrl": _IDLE_URL,
            "surfaceId": self.surface_id,
            "surfaceTargets": {self.surface_id: ready["target_id"]},
            "createdAt": _utc_now(),
        }
        self.descriptor_path = temporary / "browser-host.json"
        self.descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        try:
            os.chmod(self.descriptor_path, 0o600)
        except OSError:
            pass

    def operation(self, operation: str, *, detect_capabilities: bool = False) -> dict[str, Any]:
        if operation not in {"inspect", "verify", "smoke"} or self.descriptor_path is None or self.helper is None or self.bun is None:
            raise RuntimeError("pinned_browser_operation_invalid")
        identity = f"{operation}-{secrets.token_hex(12)}"
        child = subprocess.Popen(
            [str(self.bun), str(self.helper)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ,
                 "CODEX_CHATGPT_WEB_BROWSER_HELPER_PROCESS": "1",
                 "CODEX_CHATGPT_WEB_HOME": str(self.temporary.name if self.temporary else self.home)},
        )
        try:
            assert child.stdin is not None and child.stdout is not None
            lines = _OutputLines(child.stdout)
            ready = _read_line(child, lines, 10)
            if ready.get("type") != "ready":
                raise RuntimeError("pinned_browser_helper_protocol_invalid")
            message: dict[str, Any] = {
                "type": operation,
                "id": identity,
                "config": {"appName": str(self.config.get("automaticAppName") or self.config.get("appName") or runtime.CONNECTOR_NAME),
                           "browserHostDescriptorPath": str(self.descriptor_path)},
            }
            if operation == "inspect":
                message["detectCapabilities"] = detect_capabilities
            child.stdin.write(json.dumps(message) + "\n")
            child.stdin.flush()
            result = _read_until_result(child, lines, identity,
                                        _SMOKE_OPERATION_TIMEOUT if operation == "smoke" else _HELPER_OPERATION_TIMEOUT)
            child.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
            child.stdin.flush()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=3)
            if result.get("type") == "error":
                raise RuntimeError(_safe_helper_error(result))
            if result.get("type") != "result":
                raise RuntimeError("pinned_browser_helper_protocol_invalid")
            return result
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)

    def close(self) -> None:
        if self.bootstrap is not None and self.bootstrap.poll() is None:
            if self.bootstrap.stdin is not None:
                self.bootstrap.stdin.write("close\n")
                self.bootstrap.stdin.flush()
            try:
                self.bootstrap.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bootstrap.terminate()
                self.bootstrap.wait(timeout=3)
        if self.browser is not None and self.browser.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.browser.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(self.browser.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                self.browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.browser.kill()
                self.browser.wait(timeout=3)
        if self.control is not None:
            self.control.shutdown()
            self.control.server_close()
        if self.control_thread is not None:
            self.control_thread.join(timeout=2)
        if self.temporary is not None:
            self.temporary.cleanup()


def _find_bun(root: Path) -> Path | None:
    names = {"bun", "bun.exe"}
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() in names:
            return path
    return None


def _wait_for_cdp(active_port: Path, process: subprocess.Popen[bytes]) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("managed_browser_start_failed")
        try:
            port = int(active_port.read_text(encoding="utf-8").splitlines()[0])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            ws = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
            if isinstance(ws, str) and ws.startswith(f"ws://127.0.0.1:{port}/"):
                return f"http://127.0.0.1:{port}"
        except (OSError, ValueError, IndexError, json.JSONDecodeError):
            time.sleep(0.1)
    raise RuntimeError("managed_browser_cdp_unavailable")


def _bootstrap_source() -> str:
    return r'''const { chromium } = require(process.env.CODEXHUB_CHECK_PLAYWRIGHT);
const readline = require("node:readline");
(async () => {
  const browser = await chromium.connectOverCDP(process.env.CODEXHUB_CHECK_CDP);
  const context = await browser.newContext({ storageState: process.env.CODEXHUB_CHECK_STATE });
  const page = await context.newPage();
  const session = await context.newCDPSession(page);
  const { targetInfo } = await session.send("Target.getTargetInfo");
  console.log(JSON.stringify({ ok: true, target_id: targetInfo.targetId }));
  const input = readline.createInterface({ input: process.stdin });
  input.once("line", async () => {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
    process.exit(0);
  });
})().catch(error => {
  console.log(JSON.stringify({ ok: false, error: error instanceof Error ? error.name : "Error" }));
  process.exit(1);
});
'''


def _read_line(child: subprocess.Popen[str], stream: Any, timeout: float) -> dict[str, Any]:
    try:
        line = stream.read(timeout)
    except queue.Empty:
        raise RuntimeError("pinned_browser_operation_timed_out")
    if not line:
        raise RuntimeError("pinned_browser_process_exited")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("pinned_browser_helper_protocol_invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("pinned_browser_helper_protocol_invalid")
    return payload


class _OutputLines:
    def __init__(self, stream: Any):
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream: Any) -> None:
        try:
            for line in stream:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def read(self, timeout: float) -> str | None:
        return self.lines.get(timeout=timeout)


def _read_until_result(child: subprocess.Popen[str], stream: _OutputLines, identity: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("pinned_browser_process_exited")
        message = _read_line(child, stream, max(0.01, deadline - time.monotonic()))
        if message.get("id") != identity or message.get("type") not in {"result", "error"}:
            raise RuntimeError("pinned_browser_helper_protocol_invalid")
        return message
    raise RuntimeError("pinned_browser_operation_timed_out")


def _safe_helper_error(message: dict[str, Any]) -> str:
    detail = message.get("message")
    if isinstance(detail, str):
        lowered = detail.casefold()
        if ("authentication could not be verified" in lowered
                or "not authenticated" in lowered
                or "no visible composer is present" in lowered
                or "sign in to continue" in lowered):
            return "not_authenticated"
        if "session has expired" in lowered or "session expired" in lowered:
            return "session_expired"
    name = message.get("name")
    if isinstance(name, str) and name.startswith("ChatGpt"):
        return "browser_check_failed"
    return "browser_check_failed"


def _error_result(reason: str) -> dict[str, Any]:
    return {"state": "failed", "reason": reason}


def check_runtime(home: Path) -> dict[str, Any]:
    """Run explicit, read-only browser/login/connector/model checks on the active instance."""
    home = runtime._assert_private_home(Path(home))
    binding = _binding(home)
    config = runtime._read_json(runtime._web_home(home) / "config.json") or {}
    result = _empty_result(cache_state="current", reason="check_incomplete")
    result["checked_at"] = _utc_now()
    if binding is None:
        result.update(state="blocked", reason="active_account_state_unavailable")
        return result
    storage_path = _storage_state_path(home, config)
    try:
        initial_state_digest = _sha256(storage_path.read_bytes())
    except OSError:
        result.update(state="blocked", reason="active_account_state_unavailable")
        return result
    if _binding(home) != binding:
        result.update(state="stale", cache_state="stale", reason="active_config_or_account_changed_during_check")
        return result
    try:
        pin = runtime.load_pin()
        if not runtime._pin_compatible(home, pin):
            result.update(state="blocked", reason="runtime_pin_incompatible")
            _save_result(home, binding, result)
            return result
    except RuntimeError:
        result.update(state="blocked", reason="runtime_pin_unavailable")
        _save_result(home, binding, result)
        return result
    record = runtime._process_record(home)
    if record is None or not isinstance(record.get("port"), int):
        result.update(state="blocked", reason="runtime_not_running")
        _save_result(home, binding, result)
        return result
    port = int(record["port"])
    token = config.get("controlToken") if isinstance(config.get("controlToken"), str) else ""
    try:
        control_status = read_control_status(port, token)
    except RuntimeError as exc:
        reason = str(exc)
        if reason not in {"runtime_control_contract_unsupported", "runtime_control_contract_invalid"}:
            reason = "runtime_control_contract_unavailable"
        result.update(state="blocked", reason=reason)
        _save_if_binding_unchanged(home, binding, result)
        return result
    result["active_turns"] = control_status["active_turns"]
    result["runtime_capabilities"] = control_status["account_capabilities"]
    result["models"] = control_status["models"]
    result["model_state"] = "available" if result["models"] else "none"
    if not control_status["accepting_turns"]:
        result.update(state="blocked", reason="runtime_not_accepting_turns")
        _save_if_binding_unchanged(home, binding, result)
        return result
    http_turns = result["active_turns"]["http"]
    browser_turns = result["active_turns"]["browser"]
    if http_turns or browser_turns:
        result.update(state="blocked", reason="active_turns")
        _save_if_binding_unchanged(home, binding, result)
        return result

    try:
        with _PinnedBrowserSession(home, config) as browser:
            try:
                inspected = browser.operation("inspect", detect_capabilities=True).get("value")
                if isinstance(inspected, dict) and inspected.get("authenticated") is True and inspected.get("temporary") is True:
                    account_key = inspected.get("accountKey")
                    if not isinstance(account_key, str) or _ACCOUNT_KEY_RE.fullmatch(account_key) is None:
                        result["login"] = _error_result("account_identity_unverified")
                    else:
                        try:
                            current_state_digest = _sha256(storage_path.read_bytes())
                        except OSError:
                            current_state_digest = ""
                        if current_state_digest != initial_state_digest or _binding(home) != binding:
                            result.update(
                                state="stale",
                                cache_state="stale",
                                reason="active_config_or_account_changed_during_check",
                            )
                            return result
                        if not _write_account_identity_attestation(
                            storage_path, account_key, initial_state_digest
                        ):
                            result["login"] = _error_result("account_identity_unverified")
                        else:
                            attested_binding = _binding(home)
                            try:
                                attested_state = storage_path.read_bytes()
                            except OSError:
                                attested_state = b""
                            if (
                                attested_binding is None
                                or attested_binding["active_config_sha256"] != binding["active_config_sha256"]
                                or attested_binding["runtime_instance_sha256"] != binding["runtime_instance_sha256"]
                                or _attested_account_key(storage_path, attested_state) != account_key
                            ):
                                result.update(
                                    state="stale",
                                    cache_state="stale",
                                    reason="active_config_or_account_changed_during_check",
                                )
                                return result
                            binding = attested_binding
                            capabilities = {
                                key: inspected.get(key) is True
                                for key in ("solAvailable", "extraHighAvailable", "proAvailable")
                                if isinstance(inspected.get(key), bool)
                            }
                            result["login"] = {"state": "signed_in", "capabilities": capabilities}
                            if len(capabilities) == 3:
                                matches = capabilities == result["runtime_capabilities"]
                                result["capabilities_match"] = matches
                                if not matches:
                                    result["models"] = []
                                    result["model_state"] = "capabilities_mismatch"
                else:
                    result["login"] = _error_result("session_unconfirmed")
            except RuntimeError as exc:
                reason = str(exc)
                if reason in {"not_authenticated", "session_expired"}:
                    result["login"] = {"state": "signed_out", "reason": reason, "capabilities": {}}
                else:
                    result["login"] = _error_result(reason)

            if result["login"]["state"] == "signed_in":
                try:
                    selected = browser.operation("verify").get("text")
                    expected = config.get("automaticAppName", config.get("appName", runtime.CONNECTOR_NAME))
                    if selected == expected:
                        result["connector"] = {"state": "selectable", "name": selected}
                    else:
                        result["connector"] = _error_result("connector_name_mismatch")
                except RuntimeError as exc:
                    result["connector"] = _error_result(str(exc))
                try:
                    smoke = browser.operation("smoke").get("value")
                    if isinstance(smoke, dict) and isinstance(smoke.get("response"), str):
                        result["browser"] = {"state": "passed"}
                    else:
                        result["browser"] = _error_result("smoke_result_invalid")
                except RuntimeError as exc:
                    result["browser"] = _error_result(str(exc))
            else:
                result["browser"] = {"state": "not_checked", "reason": "login_required"}
                result["connector"] = {"state": "not_checked", "reason": "login_required"}
    except (OSError, RuntimeError) as exc:
        reason = str(exc) if str(exc).startswith(("pinned_", "managed_")) else "browser_check_unavailable"
        result["browser"] = _error_result(reason)
        result["login"] = _error_result(reason)
        result["connector"] = _error_result(reason)

    try:
        doctor = runtime._run_doctor(home, runtime._find_entry(runtime._runtime_root(home)))
    except (OSError, RuntimeError):
        doctor = None
    tunnel_ok = _doctor_tunnel_ready(doctor)
    result["tunnel"] = {"state": "ready" if tunnel_ok else "not_ready"}
    text_ready = (
        result["login"]["state"] == "signed_in"
        and result["browser"]["state"] == "passed"
        and bool(result["models"])
        and result["capabilities_match"] is True
    )
    tools_ready = (
        text_ready
        and result["connector"]["state"] == "selectable"
        and config.get("mode") == "full"
        and tunnel_ok
    )
    result["text_ready"] = bool(text_ready)
    result["tools_ready"] = bool(tools_ready)
    result["state"] = "ready" if text_ready and tools_ready else "partial" if text_ready else "failed"
    result["reason"] = None if result["state"] == "ready" else (
        "tools_not_ready" if text_ready else
        "account_capabilities_changed" if result["capabilities_match"] is False else
        "readiness_evidence_incomplete"
    )
    _save_if_binding_unchanged(home, binding, result)
    return result


def _doctor_tunnel_ready(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict):
        return False
    checks = report.get("checks")
    if not isinstance(checks, list):
        return False
    return any(
        isinstance(check, dict) and check.get("id") == "tunnel-runtime" and check.get("status") == "ok"
        for check in checks
    )


def _save_if_binding_unchanged(home: Path, binding: dict[str, str], result: dict[str, Any]) -> None:
    if _binding(home) != binding:
        result.update(state="stale", cache_state="stale", text_ready=False, tools_ready=False,
                      reason="active_config_or_account_changed_during_check")
        return
    _save_result(home, binding, {key: value for key, value in result.items() if key != "cache_state"})


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "check"))
    parser.add_argument("--home", type=Path, default=runtime.default_home())
    args = parser.parse_args(argv)
    try:
        result = cached_checks(args.home) if args.action == "status" else check_runtime(args.home)
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "state": "failed", "reason": "check_failed"}, sort_keys=True) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
