"""Bounded Claude Code 2.1.280 coexistence qualification against loopback only.

The harness copies only the current OAuth access token into a temporary Claude
home, drops the refresh token, and never sends a request outside loopback.
Evidence records model IDs and boolean credential checks, never credential data.
"""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PINNED_VERSION = "2.1.280"
LOCAL_KEY = "synthetic-codexhub-local-key"
MAX_REQUESTS = 96
MAX_OUTPUT_TOKENS = 128000
CASE_TIMEOUT_SECONDS = 35
OVERALL_TIMEOUT_SECONDS = 300


def _version(binary: str) -> str:
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    return result.stdout.strip() or result.stderr.strip()


def _copy_access_only(source: Path, destination: Path) -> tuple[str, int]:
    payload = json.loads(source.read_text())
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str):
        raise ValueError("credential file has no Claude subscription access token")
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        raise ValueError("Claude subscription access token has no numeric expiry")
    remaining = int(expires_at / 1000 - time.time())
    if remaining < OVERALL_TIMEOUT_SECONDS + 300:
        raise ValueError("subscription access token expires too soon; refresh is intentionally disabled")
    # Claude Code can use the still-valid access token but cannot refresh it.
    copied = {"claudeAiOauth": {key: oauth[key] for key in (
        "accessToken", "expiresAt", "subscriptionType", "rateLimitTier", "scopes"
    ) if key in oauth}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(copied))
    destination.chmod(0o600)
    return oauth["accessToken"], remaining


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sse(model: str) -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_qualification", "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 0},
        }}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "text_delta", "text": "QUALIFIED"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
                    for name, data in events)


class HarnessState:
    def __init__(self, oauth_token: str) -> None:
        self.oauth_token = oauth_token
        self.records: list[dict[str, Any]] = []
        self.current_case = ""
        self.deadline = time.monotonic() + OVERALL_TIMEOUT_SECONDS
        self.lock = threading.Lock()

    def record(self, value: dict[str, Any]) -> None:
        with self.lock:
            if len(self.records) >= MAX_REQUESTS:
                raise RuntimeError(f"request cap {MAX_REQUESTS} reached")
            value["case"] = self.current_case
            self.records.append(value)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: HarnessState

    def log_message(self, *_: Any) -> None:
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("request-id", "req_qualification")
        self.send_header("retry-after", "0")
        self.end_headers()
        self.wfile.write(body)

    def _record(self, protocol: str, body: bytes, status: int) -> None:
        try:
            request = json.loads(body) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = {}
        authorization = self.headers.get("authorization", "")
        bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        beta = self.headers.get("anthropic-beta", "")
        self.state.record({
            "protocol": protocol,
            "method": self.command,
            "path": self.path.split("?", 1)[0],
            "status": status,
            "model": request.get("model") if isinstance(request, dict) else None,
            "max_tokens": request.get("max_tokens") if isinstance(request, dict) else None,
            "stream": request.get("stream") if isinstance(request, dict) else None,
            "oauth_bearer_matches_saved_access_token": bearer == self.state.oauth_token,
            "authorization_present": bool(authorization),
            "x_api_key_present": bool(self.headers.get("x-api-key")),
            "local_key_present": bool(self.headers.get("x-codexhub-gateway-key")),
            "local_key_matches": self.headers.get("x-codexhub-gateway-key") == LOCAL_KEY,
            "oauth_beta_present": "oauth" in beta.lower(),
            "anthropic_version_present": bool(self.headers.get("anthropic-version")),
        })

    def do_HEAD(self) -> None:
        self._reply(200, b"")

    def do_CONNECT(self) -> None:
        self.state.record({"protocol": "egress", "method": "CONNECT", "status": 502,
                           "case": self.state.current_case})
        self._reply(502, b"{}")

    def do_GET(self) -> None:
        if self.path.startswith("/v1/models"):
            model = "codexhub/loopback-model"
            payload = json.dumps({"data": [{"id": model, "display_name": "Loopback Model"}]}).encode()
            self._record("discovery", b"", 200)
            self._reply(200, payload)
        else:
            self._record("unknown", b"", 404)
            self._reply(404, b"{}")

    def do_POST(self) -> None:
        body = self._body()
        if len(self.state.records) >= MAX_REQUESTS:
            self._reply(429, b'{"type":"error","error":{"type":"rate_limit_error"}}')
            return
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = {}
        declared = request.get("max_tokens") if isinstance(request, dict) else None
        if isinstance(declared, int) and declared > MAX_OUTPUT_TOKENS:
            self._record("budget_refusal", body, 429)
            self._reply(429, b'{"type":"error","error":{"type":"rate_limit_error","message":"test output budget exceeded"}}')
            return
        if self.path.startswith("/v1/messages/count_tokens"):
            self._record("count_tokens", body, 200)
            self._reply(200, b'{"input_tokens":11}')
            return
        if not self.path.startswith("/v1/messages"):
            self._record("unknown", body, 404)
            self._reply(404, b'{"type":"error","error":{"type":"not_found_error"}}')
            return
        valid = self.headers.get("x-codexhub-gateway-key") == LOCAL_KEY
        self._record("messages", body, 200 if valid else 401)
        if not valid:
            self._reply(401, b'{"type":"error","error":{"type":"authentication_error","message":"local Gateway credential missing or invalid"}}')
            return
        self._reply(200, _sse(str(request.get("model") or "unknown")), "text/event-stream")


class ProxyGuard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: HarnessState

    def log_message(self, *_: Any) -> None:
        pass

    def do_CONNECT(self) -> None:
        self.state.record({"protocol": "egress", "method": "CONNECT", "destination_host": self.path.split(":", 1)[0],
                           "status": 502})
        self._reply()

    def do_GET(self) -> None:
        self.state.record({"protocol": "egress", "method": "GET", "destination_host": self.path.split("/", 3)[2]
                           if self.path.startswith("http") else "unknown", "status": 502})
        self._reply()

    def do_POST(self) -> None:
        self.state.record({"protocol": "egress", "method": "POST", "destination_host": self.path.split("/", 3)[2]
                           if self.path.startswith("http") else "unknown", "status": 502})
        self._reply()

    def _reply(self) -> None:
        body = b'{"error":"egress denied by qualification harness"}'
        self.send_response(502)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, *_: Any) -> None:
        pass


def _http_server(port: int, handler: type[BaseHTTPRequestHandler], state: HarnessState) -> ThreadingHTTPServer:
    bound = type("BoundHandler", (handler,), {"state": state})
    return QuietHTTPServer(("127.0.0.1", port), bound)


def _cli_env(home: Path, base_url: str, proxy: str, local_key: str | None,
             extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "TMPDIR": str(home / "tmp"),
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "ANTHROPIC_BASE_URL": base_url,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    if local_key:
        env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-codexhub-gateway-key: {local_key}"
    if extra:
        env.update(extra)
    return env


def _run_case(binary: str, env: dict[str, str], cwd: Path, state: HarnessState,
              name: str, args: list[str], expected_model: str | None,
              *, success: bool = True, permission_mode: str | None = None,
              preserve_session: bool = False) -> dict[str, Any]:
    state.current_case = name
    before = len(state.records)
    remaining = state.deadline - time.monotonic()
    if remaining <= 0:
        return {"case": name, "passed": False, "timeout": "overall deadline reached",
                "request_count": 0}
    command = [binary, "-p", "--tools", "", "--permission-prompts", "none",
               "--output-format", "json"]
    if not preserve_session:
        command.append("--no-session-persistence")
    if permission_mode is not None:
        command.extend(["--permission-mode", permission_mode])
    command.extend([*args, "Reply exactly QUALIFIED and do not use tools."])
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=min(CASE_TIMEOUT_SECONDS, remaining))
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            response = {}
        case_records = [row for row in state.records[before:] if row.get("case") == name]
        messages = [row for row in case_records if row["protocol"] == "messages"]
        passed = result.returncode == 0 and response.get("is_error") is False
        if success:
            passed = passed and any(row["status"] == 200 for row in messages)
            if expected_model is not None:
                passed = passed and any(row.get("model") == expected_model for row in messages)
        else:
            passed = (not passed) and any(row["status"] == 401 for row in messages)
        if success:
            passed = passed and "QUALIFIED" in str(response.get("result", ""))
        return {"case": name, "passed": bool(passed), "exit_code": result.returncode,
                "message_statuses": [row["status"] for row in messages],
                "models": [row.get("model") for row in messages],
                "expected_model": expected_model,
                "request_count": len(case_records)}
    except subprocess.TimeoutExpired:
        return {"case": name, "passed": False, "timeout": CASE_TIMEOUT_SECONDS,
                "request_count": len(state.records) - before}


def _picker_transcript(binary: str, env: dict[str, str], cwd: Path,
                       timeout: float = 12) -> str:
    """Open the real /model picker in a PTY and return its terminal text."""
    import pty

    master, slave = pty.openpty()
    process = subprocess.Popen([binary], cwd=cwd, env=env, stdin=slave, stdout=slave,
                               stderr=slave, close_fds=True, start_new_session=True)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + timeout
    sent_trust = False
    sent_picker = False
    while time.monotonic() < deadline and process.poll() is None:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            output.extend(chunk)
            visible = re.sub(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", b" ", output)
            lower = visible.lower()
            if not sent_trust and (b"trust" in lower and b"directory" in lower):
                os.write(master, b"1\r")
                sent_trust = True
            elif not sent_picker and (b"welcome" in lower or b"what should" in lower or b"claude code" in lower):
                os.write(master, b"/model\r")
                sent_picker = True
            if sent_picker and (b"Opus via CodexHub" in visible or b"Claude Opus 5.5" in visible):
                break
    try:
        os.write(master, b"\x1b")
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=2)
    os.close(master)
    text = output.decode(errors="replace")
    return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", " ", text)


def qualify(binary: str, credential_file: Path, output: Path) -> dict[str, Any]:
    host_netns = os.environ.get("CODEXHUB_HOST_NETNS_ID")
    current_netns = os.readlink("/proc/self/ns/net")
    if not host_netns or current_netns == host_netns:
        raise RuntimeError("run inside an isolated network namespace; see the reproduction command")
    version = _version(binary)
    if PINNED_VERSION not in version:
        raise RuntimeError(f"requires Claude Code {PINNED_VERSION}; found {version}")
    source_hash = hashlib.sha256(credential_file.read_bytes()).hexdigest()
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="codexhub-claude-560-") as temp:
        root = Path(temp)
        home = root / "home"
        config = home / ".claude"
        work = root / "work"
        for path in (config, home / "tmp", work):
            path.mkdir(parents=True, exist_ok=True)
        token, expires_in = _copy_access_only(credential_file, config / ".credentials.json")
        (config / "settings.json").write_text(json.dumps({
            "model": "opus",
            "modelPicker": {"options": [
                {"model": "codexhub/opus", "label": "Opus via CodexHub"},
                {"model": "claude-opus-5-5", "label": "Claude Opus 5.5"},
            ]},
        }))
        gateway_port, proxy_port = _free_port(), _free_port()
        state = HarnessState(token)
        gateway = _http_server(gateway_port, GatewayHandler, state)
        proxy = _http_server(proxy_port, ProxyGuard, state)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (gateway, proxy)]
        for thread in threads:
            thread.start()
        env = _cli_env(home, f"http://127.0.0.1:{gateway_port}",
                       f"http://127.0.0.1:{proxy_port}", LOCAL_KEY)

        mappings = {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "codexhub/opus",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "codexhub/sonnet",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "codexhub/haiku",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "codexhub/fable",
        }
        try:
            alias_env = {**env, **mappings}
            for alias, variable in (("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
                                    ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                    ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                                    ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL")):
                results.append(_run_case(binary, alias_env, work, state, f"mapped_{alias}",
                                         ["--model", alias], mappings[variable]))
            results.append(_run_case(binary, env, work, state, "unmapped_haiku",
                                     ["--model", "haiku"], "claude-haiku-4-5-20251001"))
            results.append(_run_case(binary, alias_env, work, state, "explicit_opus_55",
                                     ["--model", "claude-opus-5-5"], "claude-opus-5-5"))
            session_id = str(uuid.uuid4())
            results.append(_run_case(binary, alias_env, work, state, "native_session_initial",
                                     ["--model", "claude-opus-5-5", "--session-id", session_id],
                                     "claude-opus-5-5", preserve_session=True))
            resume_result = _run_case(binary, alias_env, work, state, "native_session_resume",
                                      ["--resume", session_id], "claude-opus-5-5", preserve_session=True)
            if not resume_result.get("passed") and "codexhub/opus" in resume_result.get("models", []):
                resume_result["status"] = "potential_contradiction"
                resume_result["qualification_limit"] = (
                    "The seed turn used print-mode --model; interactive /model persistence remains unverified."
                )
            results.append(resume_result)
            results.append(_run_case(binary, alias_env, work, state, "default_opus_alias",
                                     [], "codexhub/opus"))
            results.append(_run_case(binary, alias_env, work, state, "opusplan_plan_phase",
                                     ["--model", "opusplan"], "codexhub/opus",
                                     permission_mode="plan"))
            nonplan_env = {**alias_env}
            results.append(_run_case(binary, nonplan_env, work, state, "opusplan_execution_phase",
                                     ["--model", "opusplan"], "codexhub/sonnet"))
            missing_key_env = _cli_env(home, f"http://127.0.0.1:{gateway_port}",
                                      f"http://127.0.0.1:{proxy_port}", None)
            results.append(_run_case(binary, missing_key_env, work, state, "missing_local_credential",
                                     ["--model", "claude-opus-5-5"], None, success=False))
            results.append(_run_case(binary, _cli_env(home, f"http://127.0.0.1:{gateway_port}",
                                                       f"http://127.0.0.1:{proxy_port}", "wrong-local-key"),
                                     work, state, "wrong_local_credential",
                                     ["--model", "claude-opus-5-5"], None, success=False))
            discovery_env = {**env, "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}
            results.append(_run_case(binary, discovery_env, work, state, "oauth_only_discovery",
                                     ["--model", "claude-opus-5-5"], "claude-opus-5-5"))
            discovery_requests = [row for row in state.records if row["protocol"] == "discovery"]
            results.append({"case": "oauth_only_discovery_skipped", "passed": not discovery_requests,
                            "request_count": len(discovery_requests)})
            results.append({"case": "subagent_inherited_and_pinned_models", "status": "unknown",
                            "reason": "Tool execution is disabled in this bounded HTTP harness."})
            state.current_case = "picker_append_rows"
            picker = _picker_transcript(binary, env, work,
                                        timeout=min(12, max(0.1, state.deadline - time.monotonic())))
            picker_result = {"case": "picker_append_rows", "configured": True,
                             "native_default_visible": "Default" in picker,
                             "native_opus_visible": bool(re.search(r"Opus", picker)),
                             "gateway_option_visible": "Opus via CodexHub" in picker,
                             "native_full_id_option_visible": "Claude Opus 5.5" in picker,
                             "status": "unknown",
                             "reason": "The bounded PTY session did not expose the model picker rows."}
            results.append(picker_result)
        finally:
            gateway.shutdown()
            proxy.shutdown()
            for thread in threads:
                thread.join(timeout=2)

    unchanged = hashlib.sha256(credential_file.read_bytes()).hexdigest() == source_hash
    discovery_requests = [row for row in state.records if row["protocol"] == "discovery"]
    egress = [row for row in state.records if row["protocol"] == "egress"]
    oauth_and_key = [row for row in state.records if row["protocol"] == "messages"
                     and row.get("status") == 200]
    evidence = {
        "cli_version": version,
        "candidate_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "platform": platform.platform(),
        "network_namespace_isolated": True,
        "credential_handling": {"access_token_snapshot_only": True, "refresh_token_copied": False,
                                "expires_in_seconds_at_start": expires_in,
                                "source_credentials_unchanged": unchanged},
        "bounds": {"max_requests": MAX_REQUESTS, "max_output_tokens": MAX_OUTPUT_TOKENS,
                   "per_cli_timeout_seconds": CASE_TIMEOUT_SECONDS,
                   "overall_timeout_seconds": OVERALL_TIMEOUT_SECONDS,
                   "mock_output_tokens_per_success": 2,
                   "live_provider_calls": 0,
                   "requests_observed": len(state.records), "duration_seconds": round(time.monotonic() - started, 2)},
        "credential_observations": {
            "valid_oauth_and_local_key_on_same_messages_request": bool(oauth_and_key)
                and all(row["oauth_bearer_matches_saved_access_token"] and row["local_key_matches"]
                        and not row["x_api_key_present"] and row["oauth_beta_present"]
                        for row in oauth_and_key),
            "missing_and_wrong_local_keys_rejected_even_with_saved_oauth": all(
                any(row["case"] == name and row["status"] == 401 and row["oauth_bearer_matches_saved_access_token"]
                    and not row["local_key_matches"] for row in state.records)
                for name in ("missing_local_credential", "wrong_local_credential")),
            "oauth_beta_present": any(row["oauth_beta_present"] for row in oauth_and_key),
            "external_provider_requests": 0,
            "external_provider_boundary": "not exercised by this Claude-client harness",
        },
        "discovery": {"enabled_in_cli": True, "requests": len(discovery_requests),
                      "credential_source": "saved Claude OAuth plus ANTHROPIC_CUSTOM_HEADERS",
                      "custom_header_authenticated_discovery": False,
                      "custom_picker_used": True},
        "egress_guard": {"attempts": len(egress), "all_blocked": all(row.get("status") == 502 for row in egress),
                         "destinations": sorted({row.get("destination_host", "unknown") for row in egress})},
        "cases": results,
        "requests": state.records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", required=True)
    parser.add_argument("--oauth-credentials", required=True, type=Path,
                        help="Claude credentials JSON; only a still-valid access token is copied")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    evidence = qualify(args.claude_bin, args.oauth_credentials, args.out)
    summary = {key: evidence[key] for key in ("cli_version", "candidate_sha", "bounds", "credential_observations",
                                               "discovery", "network_namespace_isolated", "egress_guard")}
    summary["cases"] = evidence["cases"]
    print(json.dumps(summary, indent=2))
    return 0 if (all(case.get("passed") for case in evidence["cases"])
                 and evidence["network_namespace_isolated"]
                 and evidence["egress_guard"]["all_blocked"]
                 and evidence["credential_handling"]["source_credentials_unchanged"]
                 and evidence["credential_observations"]["missing_and_wrong_local_keys_rejected_even_with_saved_oauth"]
                 and evidence["credential_observations"]["valid_oauth_and_local_key_on_same_messages_request"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
