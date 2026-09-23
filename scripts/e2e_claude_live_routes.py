"""Run real Claude Code through an isolated candidate Gateway and live upstreams."""

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
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    from scripts.e2e_claude_client_settings import claude_info, free_port, invoke
except ModuleNotFoundError:
    from e2e_claude_client_settings import claude_info, free_port, invoke

DEEPSEEK_MODEL = "deepseek-flash"  # Official alias currently served by DeepSeek V4.1 Flash.
_CLAUDE_OAUTH_METADATA = ("expiresAt", "scopes", "subscriptionType", "rateLimitTier")
MAX_LIVE_GENERATION_ATTEMPTS = 16
MAX_CASE_TIMEOUT_SECONDS = 120
MAX_OVERALL_TIMEOUT_SECONDS = 600
CLAUDE_OUTPUT_TOKEN_CAP = 128


def _deepseek_key_from_provider_toml(path: Path) -> str:
    try:
        providers = tomllib.loads(path.read_text(encoding="utf-8")).get("providers", [])
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise AssertionError("DeepSeek provider source is not a readable TOML configuration") from None
    matches = [
        provider for provider in providers
        if isinstance(provider, dict) and provider.get("id") == "deepseek"
    ]
    if len(matches) != 1:
        raise AssertionError("DeepSeek provider source must define exactly one official provider")
    provider = matches[0]
    endpoint = urlsplit(str(provider.get("base_url") or ""))
    if endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com":
        raise AssertionError("DeepSeek provider source must use the official HTTPS API")
    value = provider.get("api_key")
    if isinstance(value, str) and value.startswith("{env:") and value.endswith("}"):
        value = os.environ.get(value[5:-1], "")
    return value.strip() if isinstance(value, str) else ""


def deepseek_key(path: Path | None, provider_source: Path | None = None) -> str:
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not value and provider_source is not None:
        value = _deepseek_key_from_provider_toml(provider_source)
    if not value and path is not None:
        if path.suffix.lower() == ".toml":
            value = _deepseek_key_from_provider_toml(path)
        else:
            try:
                source = path.read_text(encoding="utf-8").strip()
                if path.suffix.lower() == ".json":
                    data = json.loads(source)
                    value = str(data.get("DEEPSEEK_API_KEY") or data.get("api_key") or "").strip()
                elif "DEEPSEEK_API_KEY=" in source:
                    value = next((line.split("=", 1)[1].strip().strip('"\'') for line in source.splitlines()
                                  if line.startswith("DEEPSEEK_API_KEY=")), "")
                else:
                    value = source
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise AssertionError("DeepSeek credential source could not be read") from None
    if not value or any(character.isspace() for character in value):
        raise AssertionError("DeepSeek official API credential is unavailable or malformed")
    return value


def snapshot_claude_subscription(
    source: Path,
    destination: Path,
    *,
    minimum_remaining_seconds: int,
) -> int:
    """Copy only Claude's current access token and allowlisted metadata.

    The refresh token is intentionally excluded so a live E2E cannot rotate the
    operator's subscription credentials.
    """
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        oauth = payload["claudeAiOauth"]
        token = oauth["accessToken"]
        expiry = oauth["expiresAt"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise AssertionError("Claude subscription source is missing current OAuth access data") from None
    if not isinstance(token, str) or not token.strip() or isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        raise AssertionError("Claude subscription source is missing current OAuth access data")
    expiry_seconds = float(expiry) / 1000 if expiry > 100_000_000_000 else float(expiry)
    remaining = int(expiry_seconds - time.time())
    if remaining <= minimum_remaining_seconds:
        raise AssertionError("Claude subscription access token expires before the bounded E2E window")
    metadata = {
        name: oauth[name]
        for name in _CLAUDE_OAUTH_METADATA
        if name in oauth and name != "expiresAt"
    }
    copied_oauth = {"accessToken": token, "expiresAt": expiry, **metadata}
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"claudeAiOauth": copied_oauth}, output, ensure_ascii=True, separators=(",", ":"))
    destination.chmod(0o600)
    return remaining


def credential_fingerprint(path: Path) -> str:
    """Return a private run-local comparison value; callers record only equality."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def provider_fixture() -> str:
    return '''[[providers]]
id = "deepseek"
name = "DeepSeek Official"
base_url = "https://api.deepseek.com"
api_key = "{env:DEEPSEEK_API_KEY}"
upstream_format = "auto"
available_upstream_formats = ["responses", "chat_completions", "anthropic_messages"]
enabled = true

  [[providers.models]]
  id = "deepseek-flash"
  display_name = "DeepSeek V4.1 Flash"
  context_window = 200000
  max_output_tokens = 8192
  enabled = true

'''


def events(bridge_port: int) -> list[dict[str, object]]:
    response = invoke(bridge_port, "gateway_recent_events", {"limit": 100})
    assert response.get("ok") is True, "Gateway event read failed"
    return response["value"]


def reserve_generation_budget(
    *,
    case: str,
    attempts: int,
    used_attempts: int,
    max_attempts: int,
    case_timeout_seconds: int,
    overall_deadline: float,
) -> tuple[int, int]:
    if attempts < 1 or used_attempts + attempts > max_attempts:
        raise AssertionError(f"live generation-attempt cap reached before {case}")
    remaining = overall_deadline - time.monotonic()
    if remaining <= 0:
        raise AssertionError(f"overall E2E deadline reached before {case}")
    timeout = int(min(case_timeout_seconds, remaining))
    if timeout < 1:
        raise AssertionError(f"no case-time budget remains before {case}")
    return used_attempts + attempts, timeout


def wait_for_event(bridge_port: int, model: str, inbound: str,
                   outbound: str, status: int = 200) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        matching = [event for event in events(bridge_port)
                    if event.get("model") in {model, f"openai/{model}"}
                    and event.get("inbound_format") == inbound
                    and event.get("upstream_format") == outbound
                    and event.get("status") == status]
        if matching:
            return
        time.sleep(0.2)
    summary = [(event.get("model"), event.get("inbound_format"),
                event.get("upstream_format"), event.get("status"))
               for event in events(bridge_port)[:12]]
    raise AssertionError(f"missing successful {inbound}->{outbound} event for {model}: {summary}")


def run_claude(
    claude_bin: Path,
    env: dict[str, str],
    root: Path,
    sentinel: str,
    *,
    model: str | None = None,
    resume: str | None = None,
    timeout_seconds: int = MAX_CASE_TIMEOUT_SECONDS,
    extra_args: tuple[str, ...] = (),
) -> dict[str, object]:
    command = [str(claude_bin), "-p", "--max-turns", "1"]
    if model:
        command.extend(("--model", model))
    if resume:
        command.extend(("--resume", resume))
    command.extend(("--permission-mode", "plan", "--output-format", "json", *extra_args,
                    f"Reply with exactly {sentinel}."))
    result = subprocess.run(
        command,
        cwd=root, env=env, capture_output=True, text=True, timeout=timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    if result.returncode != 0 or payload.get("is_error") is not False:
        status = re.search(r"\b[45]\d\d\b", str(payload.get("result", "")))
        raise AssertionError(
            f"Claude Code request failed: exit={result.returncode}, "
            f"subtype={payload.get('subtype', 'no-json')}, "
            f"error_type={payload.get('error_type')}, http_status={status.group() if status else 'unknown'}"
        )
    assert sentinel in str(payload.get("result", "")), "Claude Code response missed sentinel"
    return payload


def run_claude_launcher(launcher: Path, model: str, env: dict[str, str],
                        root: Path, sentinel: str,
                        timeout_seconds: int = MAX_CASE_TIMEOUT_SECONDS) -> None:
    result = subprocess.run(
        [str(launcher), model, "-p", "--permission-mode", "plan",
         "--output-format", "json", f"Reply with exactly {sentinel}."],
        cwd=root, env=env, capture_output=True, text=True, timeout=timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    assert result.returncode == 0 and payload.get("is_error") is False, (
        f"isolated Claude launcher failed: exit={result.returncode}, "
        f"subtype={payload.get('subtype', 'no-json')}"
    )
    assert sentinel in str(payload.get("result", "")), "Claude launcher response missed sentinel"


def run_chat(
    gateway_port: int,
    key: str,
    sentinel: str,
    timeout_seconds: int = MAX_CASE_TIMEOUT_SECONDS,
) -> None:
    request = Request(
        f"http://127.0.0.1:{gateway_port}/v1/providers/deepseek/chat/completions",
        json.dumps({"model": DEEPSEEK_MODEL, "stream": False,
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": f"Reply with exactly {sentinel}."}]}).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            assert response.status == 200
            payload = json.load(response)
    except HTTPError as error:
        try:
            body = json.load(error)
            issue = body.get("error", {})
            detail = issue.get("message", "") if isinstance(issue, dict) else ""
        except (ValueError, OSError):
            detail = ""
        lower = str(detail).lower()
        reason = next((name for marker, name in (
            ("insufficient balance", "insufficient balance"),
            ("invalid api key", "invalid API key"),
            ("model not found", "model not found"),
            ("rate limit", "rate limited"),
        ) if marker in lower), "upstream rejected request")
        raise AssertionError(f"Chat Completions endpoint returned HTTP {error.code}: {reason}") from None
    content = payload["choices"][0]["message"]["content"]
    assert sentinel in str(content), "Chat Completions response missed sentinel"


def run_deepseek_tools(
    gateway_port: int,
    key: str,
    timeout_seconds: int = MAX_CASE_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    tool = {"name": "emit_marker", "description": "Record a marker", "input_schema": {
        "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}
    cases = (
        ("providers/deepseek/chat/completions", {
            "model": "deepseek/deepseek-flash", "stream": False, "max_tokens": 256,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "Call emit_marker with value ready."}],
            "tools": [{"type": "function", "function": {"name": "emit_marker",
                       "description": tool["description"], "parameters": tool["input_schema"]}}],
            "tool_choice": {"type": "function", "function": {"name": "emit_marker"}},
        }, lambda body: any(call.get("function", {}).get("name") == "emit_marker"
                            for call in body.get("choices", [{}])[0].get("message", {}).get("tool_calls", []))),
        ("chat/completions", {
            "model": "deepseek/deepseek-flash", "stream": False, "max_tokens": 256,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "Call emit_marker with value ready."}],
            "tools": [{"type": "function", "function": {"name": "emit_marker",
                       "description": tool["description"], "parameters": tool["input_schema"]}}],
            "tool_choice": {"type": "function", "function": {"name": "emit_marker"}},
        }, lambda body: any(call.get("function", {}).get("name") == "emit_marker"
                            for call in body.get("choices", [{}])[0].get("message", {}).get("tool_calls", []))),
        ("messages", {
            "model": "deepseek/deepseek-flash", "stream": False, "max_tokens": 256,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "Call emit_marker with value ready."}],
            "tools": [tool], "tool_choice": {"type": "tool", "name": "emit_marker"},
        }, lambda body: any(block.get("type") == "tool_use" and block.get("name") == "emit_marker"
                            for block in body.get("content", []))),
    )
    for path, payload, expected in cases:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise AssertionError("DeepSeek tool case exceeded its time bound")
        request = Request(
            f"http://127.0.0.1:{gateway_port}/v1/{path}", json.dumps(payload).encode(),
            {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urlopen(request, timeout=min(timeout_seconds, remaining)) as response:
                assert response.status == 200
                body = json.load(response)
        except HTTPError as error:
            try:
                issue = json.load(error).get("error", {})
                detail = issue.get("message", "") if isinstance(issue, dict) else ""
            except (ValueError, OSError):
                detail = ""
            detail = str(detail).replace(key, "[REDACTED]")
            detail = detail.replace(os.environ.get("DEEPSEEK_API_KEY", "\0"), "[REDACTED]")
            raise AssertionError(
                f"DeepSeek {path} tool call returned HTTP {error.code}: {detail[:200]}"
            ) from None
        assert expected(body), f"DeepSeek {path} response missed emit_marker tool call"
        print(f"PASS: DeepSeek {path} tool call", flush=True)


def run_responses(
    gateway_port: int,
    key: str,
    sentinel: str,
    model: str = "gpt-6-luna",
    timeout_seconds: int = MAX_CASE_TIMEOUT_SECONDS,
) -> None:
    request = Request(
        f"http://127.0.0.1:{gateway_port}/v1/responses",
        json.dumps({"model": model, "stream": False,
                    **({"reasoning": {"effort": "max"}} if model == "gpt-6-luna" else {}),
                    "max_output_tokens": 128,
                    "input": f"Reply with exactly {sentinel}."}).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            assert response.status == 200
            payload = json.load(response)
    except HTTPError as error:
        raise AssertionError(f"Responses endpoint returned HTTP {error.code}") from None
    text = " ".join(
        content.get("text", "")
        for item in payload.get("output", []) if item.get("type") == "message"
        for content in item.get("content", []) if content.get("type") == "output_text"
    )
    assert sentinel in text, "Responses output missed sentinel"


def run(binary: Path, resource_root: Path, claude_bin: Path, auth: Path, catalog: Path,
        key: str, selected: set[str], preflight_only: bool,
        claude_subscription_source: Path | None = None,
        overall_timeout_seconds: int = 600,
        case_timeout_seconds: int = MAX_CASE_TIMEOUT_SECONDS,
        max_attempts: int = MAX_LIVE_GENERATION_ATTEMPTS,
        source_fingerprints: dict[str, tuple[Path, str]] | None = None) -> None:
    if not 1 <= max_attempts <= MAX_LIVE_GENERATION_ATTEMPTS:
        raise AssertionError("live generation-attempt cap must be between 1 and 16")
    if not 1 <= case_timeout_seconds <= MAX_CASE_TIMEOUT_SECONDS:
        raise AssertionError("per-generation timeout must be between 1 and 120 seconds")
    if not 1 <= overall_timeout_seconds <= MAX_OVERALL_TIMEOUT_SECONDS:
        raise AssertionError("overall live timeout must be between 1 and 600 seconds")
    overall_deadline = time.monotonic() + overall_timeout_seconds
    used_attempts = 0
    for path in (binary, claude_bin, auth, catalog):
        if not path.is_file():
            raise AssertionError(f"missing E2E input: {path.name}")
    for path in (resource_root / "src-python" / "codex_proxy.py",
                 resource_root / "config" / "providers.toml"):
        if not path.is_file():
            raise AssertionError(f"missing E2E resource: {path.name}")
    with tempfile.TemporaryDirectory(prefix="codexhub-claude-live-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        runtime = root / "runtime"
        codex = root / "codex"
        config = root / ".claude"
        proxy_config = runtime / "proxy" / "config"
        model_catalog = runtime / "model-catalogs"
        for path in (codex, config, proxy_config, model_catalog):
            path.mkdir(parents=True)
        (config / "settings.json").write_text('{"theme":"dark"}')
        if claude_subscription_source is not None:
            snapshot_claude_subscription(
                claude_subscription_source,
                config / ".credentials.json",
                minimum_remaining_seconds=overall_timeout_seconds + 300,
            )
        shutil.copy2(auth, codex / "auth.json")
        (proxy_config / "providers.toml").write_text(provider_fixture())
        shutil.copy2(catalog, model_catalog / "codexhub-model-catalog.json")
        gateway_port = free_port()
        bridge_port = free_port()
        if gateway_port == bridge_port or 9099 in {gateway_port, bridge_port}:
            raise AssertionError("isolated candidate ports must be unique and must not use 9099")
        gateway_key = secrets.token_hex(32)
        (runtime / "proxy" / "settings.json").write_text(json.dumps({
            "auto_start_gateway": False,
            "auto_sync_clients": False,
            "gateway_bind_address": "127.0.0.1",
            "gateway_client_key": gateway_key,
            "gateway_enable_models": True,
            "gateway_enable_responses": True,
            "gateway_enable_chat_completions": True,
            "gateway_auto_retry_enabled": False,
            "gateway_auto_retry_max_attempts": 1,
            "gateway_request_timeout_seconds": case_timeout_seconds,
            "include_official_models": True,
            "official_disabled_models": [],
            "proxy_port": gateway_port,
        }))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CODEXHUB_CLAUDE_"))}
        env.update({
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"),
            "CODEX_HOME": str(codex),
            "CLAUDE_CONFIG_DIR": str(config),
            "CODEXHUB_CLAUDE_HOME": str(config),
            "CODEXHUB_RUNTIME_HOME": str(runtime),
            "CODEXHUB_ROLLBACK_PROVENANCE_DIR": str(root / "rollback"),
            "CODEXHUB_RESOURCE_ROOT": str(resource_root),
            "CODEXHUB_PYTHON": sys.executable,
            "CODEXHUB_PROXY_PYTHON": sys.executable,
            "DEEPSEEK_API_KEY": key,
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(CLAUDE_OUTPUT_TOKEN_CAP),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PATH": f"{claude_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        })
        bridge = subprocess.Popen(
            [str(binary), "web-bridge", "--port", str(bridge_port)],
            cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        gateway_started = False
        try:
            deadline = min(overall_deadline, time.monotonic() + 20)
            while True:
                assert bridge.poll() is None, "candidate web bridge exited"
                try:
                    listed = invoke(bridge_port, "list_gateway_clients", {"include_versions": False})
                    assert listed.get("ok") is True, listed.get("error")
                    info = next(client for client in listed["value"] if client["id"] == "claude")
                    break
                except (URLError, ConnectionError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise AssertionError("candidate web bridge did not become ready") from None
                    time.sleep(0.1)
            assert info["installed"] and info["route_mode"] == "official", (
                info["installed"], info["route_mode"], info["config_path"]
            )
            started = invoke(bridge_port, "start_proxy", {})
            assert started.get("ok") is True, "isolated Gateway failed to start"
            gateway_started = True
            deadline = min(overall_deadline, time.monotonic() + 30)
            while True:
                try:
                    with urlopen(f"http://127.0.0.1:{gateway_port}/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("isolated Gateway did not become healthy")
                time.sleep(0.1)

            if not preflight_only and any("deepseek" in case for case in selected):
                balance = invoke(bridge_port, "provider_usage", {"provider_id": "deepseek"})
                assert balance.get("ok") is True, "candidate DeepSeek balance query failed"
                quota = balance["value"]
                assert quota.get("currency") in {"CNY", "USD"}
                assert isinstance(quota.get("balance"), (int, float)) and quota["balance"] >= 0
                print("PASS: candidate DeepSeek official balance query", flush=True)

            failures: list[str] = []
            launcher = resource_root / "scripts" / "codexhub-claude-gateway.sh"
            for model, outbound, label in (
                ("claude-codexhub-deepseek-deepseek-flash", "anthropic_messages", "deepseek"),
                ("claude-codexhub-gpt-6-luna", "responses", "luna"),
            ):
                if f"claude-launcher-{label}" not in selected:
                    continue
                try:
                    assert launcher.is_file(), "candidate Claude launcher is missing"
                    client_env = {name: value for name, value in env.items()
                                  if name != "DEEPSEEK_API_KEY"}
                    client_env["CODEXHUB_GATEWAY_SETTINGS"] = str(runtime / "proxy" / "settings.json")
                    if not preflight_only:
                        used_attempts, timeout_seconds = reserve_generation_budget(
                            case=f"claude-launcher-{label}", attempts=1,
                            used_attempts=used_attempts, max_attempts=max_attempts,
                            case_timeout_seconds=case_timeout_seconds,
                            overall_deadline=overall_deadline,
                        )
                        run_claude_launcher(launcher, model, client_env, root,
                                            f"CLAUDE_LAUNCHER_{label.upper()}_OK",
                                            timeout_seconds=timeout_seconds)
                        wait_for_event(bridge_port,
                                       "deepseek/deepseek-flash" if label == "deepseek" else "gpt-6-luna",
                                       "anthropic_messages", outbound)
                    assert json.loads((config / "settings.json").read_text()) == {"theme": "dark"}
                    print(f"PASS: isolated launcher -> {label}; native Claude settings unchanged",
                          flush=True)
                except (AssertionError, HTTPError) as error:
                    failures.append(f"launcher-{label}: {error}")
                    print(f"FAIL: launcher-{label}: {error}", flush=True)
            for model, outbound, label in (
                ("deepseek/deepseek-flash", "anthropic_messages", "deepseek"),
                ("gpt-6-luna", "responses", "luna"),
            ):
                if f"claude-{label}" not in selected:
                    continue
                try:
                    applied = invoke(bridge_port, "switch_gateway_client_route", {
                        "client_id": "claude", "mode": "hub", "model": model,
                        "role_mappings": {},
                    })
                    assert applied.get("ok") is True, (
                        f"Claude Connect failed for {label}: {applied.get('error')}"
                    )
                    assert claude_info(bridge_port)["claude_settings"]["default_model"] == model
                    if preflight_only:
                        print(f"PASS: isolated Claude Code settings for {label}", flush=True)
                        continue
                    client_env = {name: value for name, value in env.items()
                                  if name != "DEEPSEEK_API_KEY"}
                    timeout_seconds = case_timeout_seconds
                    if not preflight_only:
                        used_attempts, timeout_seconds = reserve_generation_budget(
                            case=f"claude-{label}", attempts=1,
                            used_attempts=used_attempts, max_attempts=max_attempts,
                            case_timeout_seconds=case_timeout_seconds,
                            overall_deadline=overall_deadline,
                        )
                    run_claude(claude_bin, client_env, root,
                               f"CLAUDE_LIVE_{label.upper().replace('-', '_')}_OK",
                               model=model,
                               timeout_seconds=timeout_seconds)
                    wait_for_event(bridge_port, model, "anthropic_messages", outbound)
                    print(f"PASS: Claude Code -> Anthropic Messages -> {label} {outbound}", flush=True)
                except (AssertionError, HTTPError) as error:
                    if label.startswith("deepseek") and "429" in str(error):
                        wait_for_event(bridge_port, model, "anthropic_messages", outbound, 429)
                    failures.append(f"{label}: {error}")
                    print(f"FAIL: {label}: {error}", flush=True)

            if "chat-deepseek" in selected and not preflight_only:
                try:
                    used_attempts, timeout_seconds = reserve_generation_budget(
                        case="chat-deepseek", attempts=1,
                        used_attempts=used_attempts, max_attempts=max_attempts,
                        case_timeout_seconds=case_timeout_seconds,
                        overall_deadline=overall_deadline,
                    )
                    run_chat(gateway_port, gateway_key, "DEEPSEEK_CHAT_LIVE_OK", timeout_seconds)
                    wait_for_event(bridge_port, "deepseek/deepseek-flash",
                                   "chat_completions", "chat_completions")
                    print("PASS: Chat Completions endpoint -> DeepSeek Official V4.1 Flash", flush=True)
                except AssertionError as error:
                    failures.append(f"deepseek-chat: {error}")
                    print(f"FAIL: deepseek-chat: {error}", flush=True)
            if "tools-deepseek" in selected and not preflight_only:
                try:
                    used_attempts, timeout_seconds = reserve_generation_budget(
                        case="tools-deepseek", attempts=3,
                        used_attempts=used_attempts, max_attempts=max_attempts,
                        case_timeout_seconds=case_timeout_seconds,
                        overall_deadline=overall_deadline,
                    )
                    run_deepseek_tools(gateway_port, gateway_key, timeout_seconds)
                except AssertionError as error:
                    failures.append(f"deepseek-tools: {error}")
                    print(f"FAIL: deepseek-tools: {error}", flush=True)
            if "responses-luna" in selected and not preflight_only:
                try:
                    used_attempts, timeout_seconds = reserve_generation_budget(
                        case="responses-luna", attempts=1,
                        used_attempts=used_attempts, max_attempts=max_attempts,
                        case_timeout_seconds=case_timeout_seconds,
                        overall_deadline=overall_deadline,
                    )
                    run_responses(gateway_port, gateway_key, "LUNA_MAX_RESPONSES_OK",
                                  timeout_seconds=timeout_seconds)
                    wait_for_event(bridge_port, "gpt-6-luna", "responses", "responses")
                    print("PASS: Responses endpoint -> Codex Luna max", flush=True)
                except AssertionError as error:
                    failures.append(f"luna-max-responses: {error}")
                    print(f"FAIL: luna-max-responses: {error}", flush=True)
            if "responses-deepseek" in selected and not preflight_only:
                try:
                    used_attempts, timeout_seconds = reserve_generation_budget(
                        case="responses-deepseek", attempts=1,
                        used_attempts=used_attempts, max_attempts=max_attempts,
                        case_timeout_seconds=case_timeout_seconds,
                        overall_deadline=overall_deadline,
                    )
                    run_responses(gateway_port, gateway_key, "DEEPSEEK_RESPONSES_LIVE_OK",
                                  "deepseek/deepseek-flash", timeout_seconds)
                    wait_for_event(bridge_port, "deepseek/deepseek-flash", "responses", "responses")
                    print("PASS: Responses endpoint -> DeepSeek Official V4.1 Flash", flush=True)
                except AssertionError as error:
                    failures.append(f"deepseek-responses: {error}")
                    print(f"FAIL: deepseek-responses: {error}", flush=True)
            detached = invoke(bridge_port, "switch_gateway_client_route", {
                "client_id": "claude", "mode": "official", "role_mappings": {},
            })
            assert detached.get("ok") is True and claude_info(bridge_port)["route_mode"] == "official"
            assert json.loads((config / "settings.json").read_text()) == {"theme": "dark"}
            assert not failures, "; ".join(failures)
        finally:
            if gateway_started:
                invoke(bridge_port, "stop_proxy", {})
            bridge.terminate()
            try:
                bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait(timeout=5)
            changed_sources = [
                name
                for name, (path, before) in (source_fingerprints or {}).items()
                if credential_fingerprint(path) != before
            ]
            if changed_sources:
                raise AssertionError(
                    "source credential files changed during isolated E2E: "
                    + ", ".join(sorted(changed_sources))
                )


def main() -> None:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--resource-root", type=Path,
                        help="candidate resource directory; defaults to the checkout")
    parser.add_argument("--claude-bin", type=Path, default=shutil.which("claude"))
    parser.add_argument("--auth", type=Path, default=home / ".codex" / "auth.json")
    parser.add_argument("--deepseek-key-file", type=Path)
    parser.add_argument(
        "--deepseek-provider-source",
        type=Path,
        help="read only the official DeepSeek provider api_key field in memory",
    )
    parser.add_argument("--claude-subscription-source", type=Path)
    parser.add_argument("--overall-timeout-seconds", type=int, default=600)
    parser.add_argument("--case-timeout-seconds", type=int, default=MAX_CASE_TIMEOUT_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=MAX_LIVE_GENERATION_ATTEMPTS)
    parser.add_argument("--preflight-only", action="store_true",
                        help="verify isolated configuration without external model requests")
    parser.add_argument("--catalog", type=Path,
                        default=home / ".codex" / "model-catalogs" / "codexhub-model-catalog.json")
    parser.add_argument("--case", action="append", choices=(
        "claude-deepseek", "claude-luna", "claude-launcher-deepseek",
        "claude-launcher-luna", "chat-deepseek", "tools-deepseek",
        "responses-deepseek", "responses-luna",
    ))
    args = parser.parse_args()
    selected = set(args.case or (
        "claude-deepseek", "claude-luna", "chat-deepseek", "tools-deepseek", "responses-deepseek", "responses-luna",
    ))
    if not 1 <= args.overall_timeout_seconds <= MAX_OVERALL_TIMEOUT_SECONDS:
        raise SystemExit("--overall-timeout-seconds must be between 1 and 600")
    if not 1 <= args.case_timeout_seconds <= MAX_CASE_TIMEOUT_SECONDS:
        raise SystemExit("--case-timeout-seconds must be between 1 and 120")
    if not 1 <= args.max_attempts <= MAX_LIVE_GENERATION_ATTEMPTS:
        raise SystemExit("--max-attempts must be between 1 and 16")
    key = deepseek_key(args.deepseek_key_file, args.deepseek_provider_source) if any(
        "deepseek" in case for case in selected
    ) else ""
    sources = {
        "codex_auth": args.auth.resolve(),
        "codex_catalog": args.catalog.resolve(),
    }
    if args.claude_subscription_source is not None:
        sources["claude_subscription"] = args.claude_subscription_source.resolve()
    deepseek_source = args.deepseek_provider_source or args.deepseek_key_file
    if deepseek_source is not None:
        sources["deepseek_provider"] = deepseek_source.resolve()
    source_fingerprints = {
        name: (path, credential_fingerprint(path))
        for name, path in sources.items()
        if path.is_file()
    }
    run(args.bin.resolve(), (args.resource_root or Path(__file__).resolve().parents[1]).resolve(),
        args.claude_bin.resolve(), args.auth.resolve(), args.catalog.resolve(), key,
        selected, args.preflight_only,
        args.claude_subscription_source.resolve() if args.claude_subscription_source else None,
        args.overall_timeout_seconds,
        args.case_timeout_seconds,
        args.max_attempts,
        source_fingerprints)


if __name__ == "__main__":
    main()
