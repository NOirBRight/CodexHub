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


def claude_cli_version(path: Path) -> str:
    result = subprocess.run(
        [str(path), "--version"], capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError("Claude Code version could not be read")
    return result.stdout.strip()[:120]


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


def verify_candidate_binding(
    binary: Path,
    resource_root: Path,
    source_root: Path,
    candidate_sha: str,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        raise AssertionError("candidate binding requires a full lowercase SHA")
    portable_root = binary.resolve().parent
    if resource_root.resolve() != portable_root:
        raise AssertionError("live E2E resource root must be the candidate portable directory")
    if "portable" not in portable_root.name.lower() or not portable_root.name.endswith(candidate_sha[:8]):
        raise AssertionError("candidate portable directory name does not match the requested SHA")
    head = subprocess.run(
        ["git", "-C", str(source_root.resolve()), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()
    if head != candidate_sha:
        raise AssertionError("source checkout HEAD does not match the candidate SHA")
    packaged_resources = (
        "config/providers.toml",
        "src-python/codex_proxy.py",
        "src-python/gateway_events.py",
        "src-python/gateway_relay_anthropic.py",
    )
    for relative in packaged_resources:
        packaged = portable_root / relative
        source = source_root.resolve() / relative
        if not packaged.is_file() or not source.is_file():
            raise AssertionError(f"candidate package is missing required resource: {relative}")
        if credential_fingerprint(packaged) != credential_fingerprint(source):
            raise AssertionError(f"candidate package resource differs from its source SHA: {relative}")


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


def _usage_summary_evidence(snapshot: dict[str, object]) -> dict[str, object]:
    summary = snapshot.get("summary")
    assert isinstance(summary, dict), "Usage Statistics snapshot has no summary"
    return {
        name: summary.get(name)
        for name in (
            "requests", "successful_requests", "missing_usage_requests",
            "partial_usage_requests", "total_tokens", "input_tokens",
            "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "cache_hit_rate",
        )
    }


def usage_evidence(
    snapshot: dict[str, object],
    *,
    case: str,
    model: str,
    provider: str,
) -> dict[str, object]:
    events = snapshot.get("events")
    assert isinstance(events, list), "Usage Statistics snapshot has no event list"
    accepted_models = {model, f"anthropic/{model}", f"openai/{model}"}
    event = next((item for item in events
                  if isinstance(item, dict)
                  and item.get("model") in accepted_models
                  and item.get("upstream") == provider
                  and item.get("status") == 200), None)
    assert event is not None, f"Usage Statistics has no successful {provider} row for {model}"
    assert event.get("usage_source") == "upstream", (
        f"Usage Statistics did not record upstream usage for {case}"
    )
    token_fields = ("input_tokens", "output_tokens", "total_tokens")
    counts = {name: event.get(name) for name in token_fields}
    assert all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in counts.values()), f"Usage Statistics has incomplete token counts for {case}"
    assert counts["input_tokens"] > 0 and counts["output_tokens"] > 0
    assert counts["total_tokens"] == counts["input_tokens"] + counts["output_tokens"]
    summary = _usage_summary_evidence(snapshot)
    assert isinstance(summary["requests"], int) and summary["requests"] >= 1
    assert isinstance(summary["total_tokens"], int) and summary["total_tokens"] >= counts["total_tokens"]
    telemetry = snapshot.get("telemetry_status")
    assert isinstance(telemetry, dict), "Usage Statistics snapshot has no telemetry status"
    assert telemetry.get("backfill_pending") is not True, "Usage Statistics persistence is still pending"
    return {
        "case": case,
        "model": event.get("model"),
        "provider": event.get("upstream"),
        "client": event.get("client_id"),
        "status": event.get("status"),
        "usage_source": event.get("usage_source"),
        "duration_ms": event.get("duration_ms"),
        **counts,
        "cached_input_tokens": event.get("cached_input_tokens"),
        "cache_write_input_tokens": event.get("cache_write_input_tokens"),
        "reasoning_tokens": event.get("reasoning_tokens"),
    }


def wait_for_usage_evidence(
    bridge_port: int,
    *,
    case: str,
    model: str,
    provider: str,
    timeout_seconds: int = 20,
) -> tuple[dict[str, object], dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = invoke(bridge_port, "gateway_usage_snapshot", {"limit": 500})
        if response.get("ok") is True and isinstance(response.get("value"), dict):
            snapshot = response["value"]
            try:
                row = usage_evidence(snapshot, case=case, model=model, provider=provider)
                return row, _usage_summary_evidence(snapshot)
            except AssertionError:
                pass
        time.sleep(0.25)
    raise AssertionError(
        f"Usage Statistics snapshot did not persist complete {provider}/{model} usage"
    )


def run_packaged_usage_statistics_window(
    binary: Path,
    env: dict[str, str],
    root: Path,
    row: dict[str, object],
    evidence_out: Path | None,
) -> tuple[bool, str | None]:
    if not os.environ.get("DISPLAY"):
        return False, "DISPLAY is unavailable; packaged window UI was not observed"
    required = ("import", "tesseract", "xwininfo", "xprop")
    if any(shutil.which(command) is None for command in required):
        return False, "X11 screenshot/OCR tooling is unavailable"
    try:
        try:
            from scripts.e2e_linux_window_input import find_codexhub_window, window_geometry, X11Harness
            from scripts.e2e_linux_window_input import terminate_process_group
        except ModuleNotFoundError:
            from e2e_linux_window_input import find_codexhub_window, window_geometry, X11Harness
            from e2e_linux_window_input import terminate_process_group
    except (ImportError, ModuleNotFoundError):
        return False, "candidate X11 window harness is unavailable"
    if evidence_out is None:
        return False, "no private output path is available for the packaged UI screenshot"
    screenshot = evidence_out.with_name(evidence_out.stem + "-packaged-usage.png")
    if screenshot.exists():
        raise AssertionError("packaged usage screenshot path already exists")
    screenshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    app_env = {
        name: value for name, value in env.items()
        if name != "DEEPSEEK_API_KEY"
    }
    app_env["GDK_BACKEND"] = "x11"
    app = subprocess.Popen(
        [str(binary)], cwd=root, env=app_env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    x11 = None
    try:
        x11 = X11Harness()
        deadline = time.monotonic() + 30
        window_id = None
        while time.monotonic() < deadline:
            if app.poll() is not None:
                raise AssertionError("packaged CodexHub window exited before rendering")
            window_id = find_codexhub_window()
            if window_id is not None:
                break
            time.sleep(0.2)
        if window_id is None:
            raise AssertionError("packaged CodexHub window did not appear")

        source_root = Path(__file__).resolve().parents[1]
        fit_stage = (source_root / "frontend/src/components/FitStage.tsx").read_text(encoding="utf-8")
        scale_match = re.search(r"FIT_STAGE_SCALE\s*=\s*([0-9.]+)", fit_stage)
        if not scale_match:
            raise AssertionError("cannot determine the current packaged UI scale")
        shadow_match = re.search(r"NATIVE_SHADOW_INSET\s*=\s*([0-9.]+)", fit_stage)
        ui_scale = float(scale_match.group(1))
        shadow_inset = float(shadow_match.group(1)) if shadow_match else 0
        app_x, app_y, _, _ = window_geometry(window_id)
        stats_x = round((shadow_inset + 22 + (96 + 8) + 48) * ui_scale)
        navigation_y = round((shadow_inset + 45 + 23) * ui_scale)
        x11.click(app_x + stats_x, app_y + navigation_y)

        def capture_text() -> str:
            screenshot.unlink(missing_ok=True)
            subprocess.run(
                ["import", "-window", f"0x{window_id:x}", str(screenshot)],
                check=True, capture_output=True, timeout=10,
            )
            result = subprocess.run(
                ["tesseract", str(screenshot), "stdout", "--psm", "6"],
                check=True, capture_output=True, text=True, timeout=20,
            )
            return result.stdout

        deadline = time.monotonic() + 15
        rendered = ""
        while time.monotonic() < deadline:
            rendered = capture_text()
            lower = rendered.lower()
            if "usage" in lower and "usage & cost" in lower:
                break
            time.sleep(0.5)
        if "usage & cost" not in rendered.lower():
            raise AssertionError("packaged window did not render the Usage Statistics page")

        # Keyboard navigation is anchored at the chart section, avoiding a
        # hard-coded screen coordinate for a control whose width is localized.
        # Tesseract gives the physical center of the rendered Request details control.
        tsv = subprocess.run(
            ["tesseract", str(screenshot), "stdout", "--psm", "6", "tsv"],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout
        request_details = _ocr_phrase_center(tsv, "request details")
        if request_details is None:
            raise AssertionError("packaged Usage page did not expose Request details")
        x11.click(app_x + request_details[0], app_y + request_details[1])
        deadline = time.monotonic() + 15
        normalized = ""
        expected_model = re.sub(r"[^a-z0-9]", "", str(row["model"]).lower())
        expected_input = re.sub(r"[^0-9]", "", str(row["input_tokens"]))
        expected_output = re.sub(r"[^0-9]", "", str(row["output_tokens"]))
        expected_provider = "claudesubscription"
        while time.monotonic() < deadline:
            rendered = capture_text()
            normalized = re.sub(r"[^a-z0-9]", "", rendered.lower())
            if all(value in normalized for value in (
                expected_model, expected_input, expected_output, expected_provider, "recorded", "200",
            )):
                break
            time.sleep(0.5)
        if not all(value in normalized for value in (
            expected_model, expected_input, expected_output, expected_provider, "recorded", "200",
        )):
            raise AssertionError("packaged Usage details did not render Claude Subscription and complete token/status data")
        return True, str(screenshot)
    finally:
        if x11 is not None:
            x11.close()
        if app.poll() is None:
            terminate_process_group(app)


def _ocr_phrase_center(tsv: str, phrase: str) -> tuple[int, int] | None:
    words: dict[tuple[str, str, str], list[tuple[int, int, int, int, str]]] = {}
    for line in tsv.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 12 or fields[0] != "5" or not fields[11].strip():
            continue
        key = (fields[2], fields[3], fields[4])
        left, top, width, height = map(int, fields[6:10])
        words.setdefault(key, []).append((left, top, width, height, fields[11].strip()))
    for line in words.values():
        line.sort(key=lambda word: word[0])
        text = " ".join(word[4] for word in line).lower()
        if phrase in text:
            left = min(word[0] for word in line)
            top = min(word[1] for word in line)
            right = max(word[0] + word[2] for word in line)
            bottom = max(word[1] + word[3] for word in line)
            return (left + right) // 2, (top + bottom) // 2
    return None


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
        source_fingerprints: dict[str, tuple[Path, str]] | None = None,
        candidate_sha: str | None = None,
        evidence_out: Path | None = None,
        claude_version: str | None = None) -> None:
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
        xdg_runtime = root / "xdg-runtime"
        proxy_config = runtime / "proxy" / "config"
        model_catalog = runtime / "model-catalogs"
        for path in (codex, config, proxy_config, model_catalog, xdg_runtime):
            path.mkdir(parents=True)
        xdg_runtime.chmod(0o700)
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
        env = {
            name: value for name, value in os.environ.items()
            if not name.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CODEXHUB_CLAUDE_"))
            and name not in {
                "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY",
            }
        }
        env.update({
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(xdg_runtime),
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
            usage_rows: list[dict[str, object]] = []
            usage_summary: dict[str, object] | None = None
            packaged_usage_ui_verified = False
            packaged_usage_ui_screenshot: str | None = None
            launcher = resource_root / "scripts" / "codexhub-claude-gateway.sh"
            if "claude-native-haiku" in selected:
                model = "claude-haiku-4-5-20251001"
                try:
                    original_default = claude_info(bridge_port)["claude_settings"]["default_model"]
                    applied = invoke(bridge_port, "switch_gateway_client_route", {
                        "client_id": "claude", "mode": "hub", "role_mappings": {},
                    })
                    assert applied.get("ok") is True, (
                        f"Claude Connect failed for native Haiku: {applied.get('error')}"
                    )
                    assert claude_info(bridge_port)["claude_settings"]["default_model"] == original_default, (
                        "Claude Connect changed the existing default model"
                    )
                    if preflight_only:
                        print("PASS: native Claude connection preserves its existing default", flush=True)
                    else:
                        if claude_subscription_source is None:
                            raise AssertionError("native Haiku requires an isolated Claude subscription snapshot")
                        used_attempts, timeout_seconds = reserve_generation_budget(
                            case="claude-native-haiku", attempts=1,
                            used_attempts=used_attempts, max_attempts=max_attempts,
                            case_timeout_seconds=case_timeout_seconds,
                            overall_deadline=overall_deadline,
                        )
                        client_env = {name: value for name, value in env.items()
                                      if name != "DEEPSEEK_API_KEY"}
                        run_claude(
                            claude_bin, client_env, root, "CLAUDE_NATIVE_HAIKU_LIVE_OK",
                            model=model, timeout_seconds=timeout_seconds,
                        )
                        wait_for_event(bridge_port, model, "anthropic_messages", "anthropic_messages")
                        row, usage_summary = wait_for_usage_evidence(
                            bridge_port, case="claude-native-haiku", model=model,
                            provider="claude_subscription",
                        )
                        usage_rows.append(row)
                        packaged_usage_ui_verified, packaged_usage_ui_screenshot = (
                            run_packaged_usage_statistics_window(
                                binary, client_env, root, row, evidence_out,
                            )
                        )
                        print(
                            "PASS: native Claude Haiku -> persisted Usage Statistics row",
                            flush=True,
                        )
                        if packaged_usage_ui_verified:
                            print("PASS: packaged CodexHub Usage page shows Claude Subscription token row", flush=True)
                        else:
                            print(f"UNVERIFIED: packaged Usage page: {packaged_usage_ui_screenshot}", flush=True)
                except (AssertionError, HTTPError) as error:
                    failures.append(f"claude-native-haiku: {error}")
                    print(f"FAIL: claude-native-haiku: {error}", flush=True)
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
            if not preflight_only and evidence_out is not None:
                if not candidate_sha or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
                    raise AssertionError("live evidence requires the exact 40-character candidate SHA")
                artifact = {
                    "schema_version": 1,
                    "candidate_sha": candidate_sha,
                    "claude_cli_version": claude_version,
                    "status": "passed",
                    "bounds": {
                        "max_live_generation_attempts": max_attempts,
                        "live_generation_attempts": used_attempts,
                        "per_generation_timeout_seconds": case_timeout_seconds,
                        "overall_timeout_seconds": overall_timeout_seconds,
                        "max_output_tokens": CLAUDE_OUTPUT_TOKEN_CAP,
                    },
                    "verified_routes": usage_rows,
                    "usage_statistics_snapshot": usage_summary,
                    "rendered_packaged_usage_statistics_ui_verified": packaged_usage_ui_verified,
                    "packaged_usage_screenshot": packaged_usage_ui_screenshot,
                    "acceptance_status": {
                        "native_haiku_through_gateway": "verified" if "claude-native-haiku" in selected else "unverified",
                        "native_haiku_persisted_usage": "verified" if usage_rows else "unverified",
                        "packaged_usage_statistics_ui": "verified" if packaged_usage_ui_verified else "unverified",
                        "picker_coexistence": "unverified",
                        "resumed_session_switching": "unverified",
                        "explicit_native_opus_5_5_identity": "unverified",
                        "deepseek_chat_messages_usage_ui": "unverified",
                        "codex_luna_responses_usage_ui": "unverified",
                        "tools_compression_cancellation_cache_hit_reuse": "unverified",
                    },
                }
                evidence_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(evidence_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump(artifact, output, ensure_ascii=True, indent=2)
                    output.write("\n")
                print(f"PASS: sanitized evidence saved to {evidence_out}", flush=True)
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
    parser.add_argument("--candidate-sha", help="exact full SHA of the built candidate")
    parser.add_argument("--source-root", type=Path,
                        default=Path(__file__).resolve().parents[1],
                        help="clean Git checkout from which the portable candidate was built")
    parser.add_argument("--evidence-out", type=Path, help="new private path for sanitized E2E evidence")
    parser.add_argument("--preflight-only", action="store_true",
                        help="verify isolated configuration without external model requests")
    parser.add_argument("--catalog", type=Path,
                        default=home / ".codex" / "model-catalogs" / "codexhub-model-catalog.json")
    parser.add_argument("--case", action="append", choices=(
        "claude-deepseek", "claude-luna", "claude-launcher-deepseek",
        "claude-launcher-luna", "chat-deepseek", "tools-deepseek",
        "responses-deepseek", "responses-luna", "claude-native-haiku",
    ))
    args = parser.parse_args()
    selected = set(args.case or ("claude-native-haiku",))
    if not 1 <= args.overall_timeout_seconds <= MAX_OVERALL_TIMEOUT_SECONDS:
        raise SystemExit("--overall-timeout-seconds must be between 1 and 600")
    if not 1 <= args.case_timeout_seconds <= MAX_CASE_TIMEOUT_SECONDS:
        raise SystemExit("--case-timeout-seconds must be between 1 and 120")
    if not 1 <= args.max_attempts <= MAX_LIVE_GENERATION_ATTEMPTS:
        raise SystemExit("--max-attempts must be between 1 and 16")
    if not args.preflight_only:
        if not args.candidate_sha or not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
            raise SystemExit("live E2E requires --candidate-sha with the built candidate's full SHA")
        if args.resource_root is None:
            raise SystemExit("live E2E requires --resource-root set to the portable candidate directory")
        if args.claude_bin is None:
            raise SystemExit("Claude Code is not available on PATH; pass --claude-bin")
        if "claude-native-haiku" in selected and args.claude_subscription_source is None:
            raise SystemExit("native Haiku E2E requires --claude-subscription-source")
        verify_candidate_binding(
            args.bin, args.resource_root, args.source_root, args.candidate_sha,
        )
    if not args.preflight_only and args.evidence_out is None:
        raise SystemExit("live E2E requires --evidence-out for the sanitized result")
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
    version = claude_cli_version(args.claude_bin.resolve()) if not args.preflight_only else None
    run(args.bin.resolve(), (args.resource_root or Path(__file__).resolve().parents[1]).resolve(),
        args.claude_bin.resolve(), args.auth.resolve(), args.catalog.resolve(), key,
        selected, args.preflight_only,
        args.claude_subscription_source.resolve() if args.claude_subscription_source else None,
        args.overall_timeout_seconds,
        args.case_timeout_seconds,
        args.max_attempts,
        source_fingerprints,
        candidate_sha=args.candidate_sha,
        evidence_out=args.evidence_out.resolve() if args.evidence_out else None,
        claude_version=version)


if __name__ == "__main__":
    main()
