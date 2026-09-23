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
_SAFE_DIAGNOSTIC_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_ERROR_CLASSES = frozenset({
    "HTTPError", "IncompleteRead", "OSError", "SseFrameTooLargeError",
    "TimeoutError", "UpstreamStreamErrorEvent", "UpstreamStreamIncompleteError",
    "URLError", "ValueError",
})
PACKAGED_UI_ISOLATION_ENV = "CODEXHUB_CLAUDE_LIVE_E2E_UI_SANDBOX"
PACKAGED_UI_ISOLATION_VALUE = "dbus-run-session+xvfb-run-v1"


class PackagedUIVerificationError(AssertionError):
    def __init__(self, failure_class: str, detail: str) -> None:
        self.failure_class = failure_class
        super().__init__(detail)


def validate_packaged_ui_isolation(environ: dict[str, str] | None = None) -> None:
    values = os.environ if environ is None else environ
    if values.get(PACKAGED_UI_ISOLATION_ENV) != PACKAGED_UI_ISOLATION_VALUE:
        raise SystemExit(
            "--packaged-ui requires the runner to be wrapped by dbus-run-session -- xvfb-run"
        )
    if not values.get("DBUS_SESSION_BUS_ADDRESS") or not values.get("DISPLAY"):
        raise SystemExit("--packaged-ui requires isolated D-Bus and Xvfb session variables")
    if any(shutil.which(command) is None for command in ("dbus-run-session", "xvfb-run")):
        raise SystemExit("--packaged-ui requires dbus-run-session and xvfb-run")
    if any(shutil.which(command) is None for command in (
        "import", "convert", "tesseract", "xwininfo", "xprop",
    )):
        raise SystemExit("--packaged-ui requires ImageMagick import/convert, tesseract, xwininfo, and xprop")


def packaged_ui_stderr_failure_class(stderr: str, return_code: int | None) -> str:
    lower = stderr.lower()
    if any(marker in lower for marker in ("cannot open display", "failed to open display")):
        return "display_backend_unavailable"
    if any(marker in lower for marker in ("failed to connect to the session bus", "unable to autolaunch")):
        return "session_bus_unavailable"
    if any(marker in lower for marker in ("singleinstance", "single-instance", "name already taken")):
        return "desktop_single_instance_collision"
    if "webkit" in lower or "webview" in lower:
        return "webview_runtime_failure"
    if return_code is None:
        return "window_not_found"
    return "process_exited_before_window"


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


def matching_gateway_route_events(
    bridge_port: int, *, model: str, inbound: str, outbound: str,
) -> list[dict[str, object]]:
    accepted_models = {model, f"openai/{model}"}
    recent = events(bridge_port)

    def expected_format(value: object, expected: str) -> bool:
        return value is None or value == expected

    def request_id(value: object) -> bool:
        return isinstance(value, str) and _SAFE_REQUEST_ID.fullmatch(value) is not None

    matching_request_ids = {
        event.get("request_id")
        for event in recent
        if isinstance(event.get("model"), str) and event["model"] in accepted_models
        and expected_format(event.get("inbound_format"), inbound)
        and expected_format(event.get("upstream_format"), outbound)
        and request_id(event.get("request_id"))
    }
    matching: list[dict[str, object]] = []
    for event in recent:
        observed_model = event.get("model")
        request_id = event.get("request_id")
        model_matches = isinstance(observed_model, str) and observed_model in accepted_models
        request_matches = observed_model is None and request_id in matching_request_ids
        if (
            (not model_matches and not request_matches)
            or not expected_format(event.get("inbound_format"), inbound)
            or not expected_format(event.get("upstream_format"), outbound)
        ):
            continue
        matching.append(event)
    return matching


def gateway_route_request_ids(
    bridge_port: int, *, model: str, inbound: str, outbound: str,
) -> list[str]:
    """Return the distinct bounded Gateway request IDs observed for one route."""
    return sorted({
        event.get("request_id")
        for event in matching_gateway_route_events(
            bridge_port, model=model, inbound=inbound, outbound=outbound,
        )
        if isinstance(event.get("request_id"), str)
        and _SAFE_REQUEST_ID.fullmatch(event["request_id"])
    })


def gateway_route_request_count(
    bridge_port: int, *, model: str, inbound: str, outbound: str,
) -> int:
    """Count distinct Gateway request IDs for a route."""
    return len(gateway_route_request_ids(
        bridge_port, model=model, inbound=inbound, outbound=outbound,
    ))


def upsert_route_request_count(
    rows: list[dict[str, object]],
    *,
    case: str,
    model: str,
    inbound: str,
    outbound: str,
    request_ids: list[str],
    successful_gateway_route_observed: bool = False,
) -> None:
    safe_ids = sorted({value for value in request_ids if _SAFE_REQUEST_ID.fullmatch(value)})
    row: dict[str, object] = {
        "case": case,
        "model": model,
        "inbound_format": inbound,
        "upstream_format": outbound,
        "observed_gateway_request_ids": len(safe_ids),
        "gateway_request_id_values": safe_ids,
        "successful_gateway_route_observed": successful_gateway_route_observed,
    }
    previous_index = next(
        (index for index, previous in enumerate(rows) if previous.get("case") == case),
        None,
    )
    if previous_index is None:
        rows.append(row)
        return
    previous = rows[previous_index]
    previous_ids = previous.get("gateway_request_id_values")
    if isinstance(previous_ids, list):
        merged_ids = sorted({
            value
            for value in (*previous_ids, *safe_ids)
            if isinstance(value, str) and _SAFE_REQUEST_ID.fullmatch(value)
        })
        row["gateway_request_id_values"] = merged_ids
        row["observed_gateway_request_ids"] = len(merged_ids)
    row["successful_gateway_route_observed"] = (
        previous.get("successful_gateway_route_observed") is True
        or successful_gateway_route_observed
    )
    rows[previous_index] = row


def total_observed_gateway_request_ids(rows: list[dict[str, object]]) -> int:
    return len({
        request_id
        for row in rows
        for request_id in row.get("gateway_request_id_values", [])
        if isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id)
    })


def route_case_acceptance_status(
    rows: list[dict[str, object]], failures: list[str], *, case: str,
) -> str:
    if any(
        row.get("case") == case and row.get("successful_gateway_route_observed") is True
        for row in rows
    ):
        return "verified"
    if any(item.startswith(f"{case}:") for item in failures):
        return "failed"
    return "unverified"


def gateway_route_diagnostics(
    bridge_port: int,
    *,
    model: str,
    inbound: str,
    outbound: str,
) -> list[dict[str, object]]:
    """Project recent Gateway events to a small, body-free diagnostic allowlist."""
    diagnostics: list[dict[str, object]] = []
    for event in matching_gateway_route_events(
        bridge_port, model=model, inbound=inbound, outbound=outbound,
    ):
        observed_model = event.get("model")
        request_id = event.get("request_id")
        model_matches = isinstance(observed_model, str)
        event_name = event.get("event")
        if not isinstance(event_name, str) or not _SAFE_DIAGNOSTIC_LABEL.fullmatch(event_name):
            continue
        row: dict[str, object] = {"event": event_name}
        if isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id):
            row["request_id"] = request_id
        row["model"] = observed_model if model_matches else model
        for name, source in (
            ("upstream", "upstream"),
            ("inbound_format", "inbound_format"),
            ("upstream_format", "upstream_format"),
        ):
            value = event.get(source)
            if isinstance(value, str) and _SAFE_DIAGNOSTIC_LABEL.fullmatch(value):
                row[name] = value
        status = event.get("status")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            row["http_status"] = status
        category = event.get("category")
        if isinstance(category, str) and category in {
            "recovery", "streaming", "tool_call_subagent", "codex_auth",
            "model_id", "external_upstream", "proxy",
        }:
            row["error_category"] = category
        error = event.get("error")
        if isinstance(error, str) and error in _SAFE_ERROR_CLASSES:
            row["error_class"] = error
        failure_class = event.get("failure_class")
        if isinstance(failure_class, str) and _SAFE_DIAGNOSTIC_LABEL.fullmatch(failure_class):
            row["failure_class"] = failure_class
        terminal_kind = event.get("terminal_kind")
        if isinstance(terminal_kind, str) and terminal_kind in {
            "success", "error", "missing", "incomplete",
        }:
            row["sse_terminal_kind"] = terminal_kind
        if (
            event_name == "request_error"
            and row.get("error_class") == "HTTPError"
            and "http_status" in row
        ):
            row["upstream_http_status"] = row["http_status"]
        diagnostics.append(row)
        if len(diagnostics) >= 12:
            break
    return diagnostics


def write_private_evidence(path: Path, artifact: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(artifact, output, ensure_ascii=True, indent=2)
        output.write("\n")


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
    diagnostics = gateway_route_diagnostics(
        bridge_port, model=model, inbound=inbound, outbound=outbound,
    )
    raise AssertionError(
        f"missing successful {inbound}->{outbound} event for {model}; "
        f"sanitized diagnostics={json.dumps(diagnostics, separators=(',', ':'))}"
    )


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
    gateway_request_ids: list[str],
) -> dict[str, object]:
    events = snapshot.get("events")
    assert isinstance(events, list), "Usage Statistics snapshot has no event list"
    accepted_models = {model, f"anthropic/{model}", f"openai/{model}"}
    observed_request_ids = {
        value for value in gateway_request_ids
        if isinstance(value, str) and _SAFE_REQUEST_ID.fullmatch(value)
    }
    assert observed_request_ids, f"Gateway observed no safe request ID for {case}"
    matching_events = [
        item for item in events
        if isinstance(item, dict)
        and item.get("model") in accepted_models
        and item.get("upstream") == provider
        and item.get("status") == 200
    ]
    event = next((item for item in matching_events
                  if item.get("request_id") in observed_request_ids), None)
    if event is None and matching_events:
        raise AssertionError(f"Usage Statistics request ID does not match the {case} Gateway route")
    assert event is not None, f"Usage Statistics has no successful {provider} row for {model}"
    request_id = event.get("request_id")
    assert isinstance(request_id, str) and _SAFE_REQUEST_ID.fullmatch(request_id), (
        f"Usage Statistics has no safe request ID for {case}"
    )
    assert event.get("usage_source") in {"upstream", "upstream_async"}, (
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
        "request_id": request_id,
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
    gateway_request_ids: list[str],
    timeout_seconds: int = 20,
) -> tuple[dict[str, object], dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    last_snapshot: dict[str, object] | None = None
    while time.monotonic() < deadline:
        response = invoke(bridge_port, "gateway_usage_snapshot", {"limit": 500})
        if response.get("ok") is True and isinstance(response.get("value"), dict):
            snapshot = response["value"]
            last_snapshot = snapshot
            try:
                row = usage_evidence(
                    snapshot, case=case, model=model, provider=provider,
                    gateway_request_ids=gateway_request_ids,
                )
                return row, _usage_summary_evidence(snapshot)
            except AssertionError:
                pass
        time.sleep(0.25)
    observed = [
        {name: item.get(name) for name in (
            "request_id", "model", "upstream", "status", "usage_source",
            "usage_missing_reason", "input_tokens", "output_tokens", "total_tokens",
        )}
        for item in (last_snapshot or {}).get("events", [])
        if isinstance(item, dict) and item.get("request_id") in gateway_request_ids
    ]
    raise AssertionError(
        f"Usage Statistics snapshot did not persist complete {provider}/{model} usage; "
        f"sanitized matching rows={json.dumps(observed, separators=(',', ':'))}"
    )


def run_packaged_usage_statistics_window(
    binary: Path,
    env: dict[str, str],
    root: Path,
    row: dict[str, object],
    evidence_out: Path | None,
) -> tuple[bool, str | None]:
    validate_packaged_ui_isolation()
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
    details_crop = screenshot.with_name(screenshot.stem + "-details.png")
    if screenshot.exists():
        raise AssertionError("packaged usage screenshot path already exists")
    screenshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    app_env = {
        name: value for name, value in env.items()
        if name != "DEEPSEEK_API_KEY"
    }
    for name in (
        "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK",
        "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "GDMSESSION",
    ):
        app_env.pop(name, None)
    app_env["XDG_SESSION_TYPE"] = "x11"
    app_env["GDK_BACKEND"] = "x11"
    app_env["GTK_USE_PORTAL"] = "0"
    stderr_path = root / "packaged-usage-ui.stderr"
    navigation_crop = screenshot.with_name(screenshot.stem + "-navigation.png")
    stderr_descriptor = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    stderr_output = os.fdopen(stderr_descriptor, "wb")
    app = subprocess.Popen(
        [str(binary)], cwd=root, env=app_env, stdout=subprocess.DEVNULL,
        stderr=stderr_output, start_new_session=True,
    )
    x11 = None
    try:
        x11 = X11Harness()
        deadline = time.monotonic() + 30
        window_id = None
        while time.monotonic() < deadline:
            if app.poll() is not None:
                stderr_output.flush()
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
                failure_class = packaged_ui_stderr_failure_class(stderr, app.returncode)
                raise PackagedUIVerificationError(
                    failure_class, "packaged CodexHub window exited before rendering",
                )
            window_id = find_codexhub_window()
            if window_id is not None:
                break
            time.sleep(0.2)
        if window_id is None:
            stderr_output.flush()
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            failure_class = packaged_ui_stderr_failure_class(stderr, app.poll())
            raise PackagedUIVerificationError(
                failure_class, "packaged CodexHub window did not appear",
            )

        app_x, app_y, app_width, app_height = window_geometry(window_id)

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

        nav_x = 0
        nav_y = min(50, max(0, app_height - 1))
        nav_width = min(900, app_width - nav_x)
        nav_height = min(70, app_height - nav_y)
        navigation_crop.unlink(missing_ok=True)
        deadline = time.monotonic() + 15
        usage_tab = None
        while time.monotonic() < deadline:
            capture_text()
            subprocess.run(
                ["convert", str(screenshot), "-crop",
                 f"{nav_width}x{nav_height}+{nav_x}+{nav_y}",
                 "-resize", "300%", str(navigation_crop)],
                check=True, capture_output=True, timeout=10,
            )
            tsv = subprocess.run(
                ["tesseract", str(navigation_crop), "stdout", "--psm", "6", "tsv"],
                check=True, capture_output=True, text=True, timeout=20,
            ).stdout
            detected = _ocr_phrase_center(tsv, "usage")
            if detected is not None:
                usage_tab = (round(detected[0] / 3), nav_y + round(detected[1] / 3))
                break
            time.sleep(0.5)
        if usage_tab is None:
            raise PackagedUIVerificationError(
                "usage_navigation_control_missing",
                "packaged window did not expose the Usage navigation control",
            )
        x11.click(app_x + usage_tab[0], app_y + usage_tab[1])
        deadline = time.monotonic() + 15
        rendered = ""
        while time.monotonic() < deadline:
            rendered = capture_text()
            lower = rendered.lower()
            if "usage & cost" in lower:
                break
            time.sleep(0.5)
        if "usage & cost" not in rendered.lower():
            raise PackagedUIVerificationError(
                "usage_statistics_page_missing",
                "packaged window did not render the Usage Statistics page",
            )

        # A tight high-contrast crop keeps the smaller Request details label legible to OCR.
        details_x = 0
        details_y = min(200, max(0, app_height - 1))
        details_width = min(600, app_width - details_x)
        details_height = min(120, app_height - details_y)
        subprocess.run(
            ["convert", str(screenshot), "-crop",
             f"{details_width}x{details_height}+{details_x}+{details_y}",
             "-resize", "400%", "-colorspace", "Gray", "-threshold", "75%",
             str(details_crop)],
            check=True, capture_output=True, timeout=10,
        )
        tsv = subprocess.run(
            ["tesseract", str(details_crop), "stdout", "--psm", "6", "tsv"],
            check=True, capture_output=True, text=True, timeout=20,
        ).stdout
        request_details = _ocr_phrase_center(tsv, "request details")
        if request_details is None:
            raise PackagedUIVerificationError(
                "request_details_control_missing",
                "packaged Usage page did not expose Request details",
            )
        x11.click(
            app_x + details_x + round(request_details[0] / 4),
            app_y + details_y + round(request_details[1] / 4),
        )
        deadline = time.monotonic() + 15
        normalized = ""
        expected_model = re.sub(r"[^a-z0-9]", "", str(row["model"]).lower())
        expected_input = re.sub(r"[^0-9]", "", str(row["input_tokens"]))
        expected_output = re.sub(r"[^0-9]", "", str(row["output_tokens"]))
        expected_provider = "claudesubscription"
        row_y = min(460, max(0, app_height - 1))
        row_height = min(170, app_height - row_y)
        row_width = min(1024, app_width)
        while time.monotonic() < deadline:
            capture_text()
            subprocess.run(
                ["convert", str(screenshot), "-crop",
                 f"{row_width}x{row_height}+0+{row_y}",
                 "-resize", "400%", "-colorspace", "Gray", "-threshold", "75%",
                 str(details_crop)],
                check=True, capture_output=True, timeout=10,
            )
            rendered = subprocess.run(
                ["tesseract", str(details_crop), "stdout", "--psm", "6"],
                check=True, capture_output=True, text=True, timeout=20,
            ).stdout
            normalized = re.sub(r"[^a-z0-9]", "", rendered.lower())
            if all(value in normalized for value in (
                expected_model, expected_input, expected_output, expected_provider, "recorded", "200",
            )):
                break
            time.sleep(0.5)
        if not all(value in normalized for value in (
            expected_model, expected_input, expected_output, expected_provider, "recorded", "200",
        )):
            raise PackagedUIVerificationError(
                "subscription_usage_details_missing",
                "packaged Usage details did not render Claude Subscription and complete token/status data",
            )
        return True, str(screenshot)
    finally:
        if x11 is not None:
            x11.close()
        if app.poll() is None:
            terminate_process_group(app)
        stderr_output.flush()
        stderr_output.close()
        navigation_crop.unlink(missing_ok=True)
        details_crop.unlink(missing_ok=True)


def _ocr_phrase_center(tsv: str, phrase: str) -> tuple[int, int] | None:
    words: dict[tuple[str, str, str], list[tuple[int, int, int, int, str]]] = {}
    for line in tsv.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 12 or fields[0] != "5" or not fields[11].strip():
            continue
        key = (fields[2], fields[3], fields[4])
        left, top, width, height = map(int, fields[6:10])
        words.setdefault(key, []).append((left, top, width, height, fields[11].strip()))
    phrase_words = [re.sub(r"[^a-z0-9]", "", word.lower()) for word in phrase.split()]
    for line in words.values():
        line.sort(key=lambda word: word[0])
        normalized = [re.sub(r"[^a-z0-9]", "", word[4].lower()) for word in line]
        match = next((
            line[index:index + len(phrase_words)]
            for index in range(len(line) - len(phrase_words) + 1)
            if normalized[index:index + len(phrase_words)] == phrase_words
        ), None)
        if match:
            left = min(word[0] for word in match)
            top = min(word[1] for word in match)
            right = max(word[0] + word[2] for word in match)
            bottom = max(word[1] + word[3] for word in match)
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
        claude_version: str | None = None,
        packaged_ui: bool = False) -> None:
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
            failure_diagnostics: list[dict[str, object]] = []
            route_request_counts: list[dict[str, object]] = []

            def record_route_failure(
                case: str,
                error: BaseException,
                *,
                model: str,
                inbound: str,
                outbound: str,
            ) -> None:
                failures.append(f"{case}: {error}")
                upsert_route_request_count(
                    route_request_counts,
                    case=case,
                    model=model,
                    inbound=inbound,
                    outbound=outbound,
                    request_ids=gateway_route_request_ids(
                        bridge_port, model=model, inbound=inbound, outbound=outbound,
                    ),
                )
                observed = gateway_route_diagnostics(
                    bridge_port, model=model, inbound=inbound, outbound=outbound,
                )
                failure_diagnostics.extend(observed)
                if not observed:
                    failure_diagnostics.append({
                        "case": case,
                        "model": model,
                        "inbound_format": inbound,
                        "upstream_format": outbound,
                        "error_category": "no_matching_gateway_event",
                        "failure_class": "request_not_observed",
                    })
                print(f"FAIL: {case}: {error}", flush=True)

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
                        gateway_request_ids = gateway_route_request_ids(
                            bridge_port, model=model,
                            inbound="anthropic_messages", outbound="anthropic_messages",
                        )
                        upsert_route_request_count(
                            route_request_counts,
                            case="claude-native-haiku",
                            model=model,
                            inbound="anthropic_messages",
                            outbound="anthropic_messages",
                            request_ids=gateway_request_ids,
                            successful_gateway_route_observed=True,
                        )
                        row, usage_summary = wait_for_usage_evidence(
                            bridge_port, case="claude-native-haiku", model=model,
                            provider="claude_subscription",
                            gateway_request_ids=gateway_request_ids,
                        )
                        usage_rows.append(row)
                        print(
                            "PASS: native Claude Haiku -> persisted Usage Statistics row",
                            flush=True,
                        )
                        if packaged_ui:
                            try:
                                packaged_usage_ui_verified, packaged_usage_ui_screenshot = (
                                    run_packaged_usage_statistics_window(
                                        binary, client_env, root, row, evidence_out,
                                    )
                                )
                            except (AssertionError, OSError, TimeoutError, subprocess.SubprocessError) as error:
                                failure_class = (
                                    error.failure_class
                                    if isinstance(error, PackagedUIVerificationError)
                                    else "ui_verification_failed"
                                )
                                failures.append(f"packaged-usage-ui: {failure_class}")
                                failure_diagnostics.append({
                                    "event": "packaged_usage_ui",
                                    "case": "claude-native-haiku",
                                    "error_category": "packaged_ui",
                                    "failure_class": failure_class,
                                })
                                print(
                                    f"FAIL: packaged Usage page verification ({failure_class})",
                                    flush=True,
                                )
                        else:
                            print("UNVERIFIED: packaged Usage page was not requested", flush=True)
                        if packaged_usage_ui_verified:
                            print("PASS: packaged CodexHub Usage page shows Claude Subscription token row", flush=True)
                        elif packaged_ui:
                            print(
                                f"UNVERIFIED: packaged Usage page: {packaged_usage_ui_screenshot}",
                                flush=True,
                            )
                except (AssertionError, HTTPError) as error:
                    record_route_failure(
                        "claude-native-haiku", error, model=model,
                        inbound="anthropic_messages", outbound="anthropic_messages",
                    )
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
                    record_route_failure(
                        f"launcher-{label}", error,
                        model="deepseek/deepseek-flash" if label == "deepseek" else "gpt-6-luna",
                        inbound="anthropic_messages", outbound=outbound,
                    )
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
                    if label == "deepseek":
                        gateway_request_ids = gateway_route_request_ids(
                            bridge_port, model="deepseek/deepseek-flash",
                            inbound="anthropic_messages", outbound="anthropic_messages",
                        )
                        upsert_route_request_count(
                            route_request_counts,
                            case="claude-deepseek",
                            model="deepseek/deepseek-flash",
                            inbound="anthropic_messages",
                            outbound="anthropic_messages",
                            request_ids=gateway_request_ids,
                            successful_gateway_route_observed=True,
                        )
                        row, usage_summary = wait_for_usage_evidence(
                            bridge_port, case="claude-deepseek",
                            model="deepseek/deepseek-flash", provider="deepseek",
                            gateway_request_ids=gateway_request_ids,
                        )
                        usage_rows.append(row)
                        print(
                            "PASS: Claude Messages -> DeepSeek Official -> persisted Usage Statistics row",
                            flush=True,
                        )
                    print(f"PASS: Claude Code -> Anthropic Messages -> {label} {outbound}", flush=True)
                except (AssertionError, HTTPError) as error:
                    record_route_failure(
                        "claude-deepseek" if label == "deepseek" else label,
                        error, model=model,
                        inbound="anthropic_messages", outbound=outbound,
                    )

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
                    gateway_request_ids = gateway_route_request_ids(
                        bridge_port, model="deepseek/deepseek-flash",
                        inbound="chat_completions", outbound="chat_completions",
                    )
                    upsert_route_request_count(
                        route_request_counts,
                        case="deepseek-chat",
                        model="deepseek/deepseek-flash",
                        inbound="chat_completions",
                        outbound="chat_completions",
                        request_ids=gateway_request_ids,
                        successful_gateway_route_observed=True,
                    )
                    row, usage_summary = wait_for_usage_evidence(
                        bridge_port, case="deepseek-chat",
                        model="deepseek/deepseek-flash", provider="deepseek",
                        gateway_request_ids=gateway_request_ids,
                    )
                    usage_rows.append(row)
                    print(
                        "PASS: Chat Completions -> DeepSeek Official V4.1 Flash "
                        "-> persisted Usage Statistics row",
                        flush=True,
                    )
                except AssertionError as error:
                    record_route_failure(
                        "deepseek-chat", error, model="deepseek/deepseek-flash",
                        inbound="chat_completions", outbound="chat_completions",
                    )
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
                    record_route_failure(
                        "deepseek-tools", error, model="deepseek/deepseek-flash",
                        inbound="chat_completions", outbound="chat_completions",
                    )
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
                    gateway_request_ids = gateway_route_request_ids(
                        bridge_port, model="gpt-6-luna",
                        inbound="responses", outbound="responses",
                    )
                    upsert_route_request_count(
                        route_request_counts,
                        case="responses-luna",
                        model="gpt-6-luna",
                        inbound="responses",
                        outbound="responses",
                        request_ids=gateway_request_ids,
                        successful_gateway_route_observed=True,
                    )
                    row, usage_summary = wait_for_usage_evidence(
                        bridge_port, case="responses-luna", model="gpt-6-luna",
                        provider="openai", gateway_request_ids=gateway_request_ids,
                    )
                    usage_rows.append(row)
                    print(
                        "PASS: Responses endpoint -> Codex Luna max -> persisted Usage Statistics row",
                        flush=True,
                    )
                except AssertionError as error:
                    record_route_failure(
                        "responses-luna", error, model="gpt-6-luna",
                        inbound="responses", outbound="responses",
                    )
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
                    record_route_failure(
                        "deepseek-responses", error, model="deepseek/deepseek-flash",
                        inbound="responses", outbound="responses",
                    )
            detached = invoke(bridge_port, "switch_gateway_client_route", {
                "client_id": "claude", "mode": "official", "role_mappings": {},
            })
            if detached.get("ok") is not True or claude_info(bridge_port)["route_mode"] != "official":
                failures.append("Claude route did not restore to official mode")
                failure_diagnostics.append({
                    "event": "route_restore",
                    "error_category": "isolated_configuration",
                    "failure_class": "official_route_not_restored",
                })
            if json.loads((config / "settings.json").read_text()) != {"theme": "dark"}:
                failures.append("isolated Claude settings changed during E2E")
                failure_diagnostics.append({
                    "event": "settings_integrity",
                    "error_category": "isolated_configuration",
                    "failure_class": "isolated_settings_changed",
                })
            changed_sources = [
                name
                for name, (path, before) in (source_fingerprints or {}).items()
                if credential_fingerprint(path) != before
            ]
            if changed_sources:
                failures.append("source credential files changed during isolated E2E")
                failure_diagnostics.append({
                    "event": "source_credential_check",
                    "error_category": "credential_integrity",
                    "failure_class": "source_file_changed",
                    "changed_source_count": len(changed_sources),
                })
            if not preflight_only and evidence_out is not None:
                if not candidate_sha or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
                    raise AssertionError("live evidence requires the exact 40-character candidate SHA")
                artifact = {
                    "schema_version": 2,
                    "candidate_sha": candidate_sha,
                    "claude_cli_version": claude_version,
                    "status": "failed" if failures else "passed",
                    "packaged_usage_ui_requested": packaged_ui,
                    "packaged_usage_ui_session": (
                        "dbus-run-session+xvfb-run" if packaged_ui else "not-requested"
                    ),
                    "bounds": {
                        "max_live_generation_attempts": max_attempts,
                        "live_generation_attempts": used_attempts,
                        "generation_attempt_budget_scope": (
                            "runner-level CLI generation invocation reservations; this is not a hard cap "
                            "on Gateway request IDs or retries performed internally by a client"
                        ),
                        "per_generation_timeout_seconds": case_timeout_seconds,
                        "overall_timeout_seconds": overall_timeout_seconds,
                        "max_output_tokens": CLAUDE_OUTPUT_TOKEN_CAP,
                    },
                    "verified_routes": usage_rows,
                    "route_request_counts": route_request_counts,
                    "total_observed_gateway_request_ids": total_observed_gateway_request_ids(
                        route_request_counts
                    ),
                    "usage_statistics_snapshot": usage_summary,
                    "failure_diagnostics": failure_diagnostics,
                    "rendered_packaged_usage_statistics_ui_verified": packaged_usage_ui_verified,
                        "packaged_usage_screenshot": packaged_usage_ui_screenshot,
                    "acceptance_status": {
                        "native_haiku_through_gateway": (
                            route_case_acceptance_status(
                                route_request_counts, failures, case="claude-native-haiku",
                            )
                        ),
                        "native_haiku_persisted_usage": (
                            "verified" if any(row.get("case") == "claude-native-haiku" for row in usage_rows)
                            else "failed" if any(item.startswith("claude-native-haiku:") for item in failures)
                            else "unverified"
                        ),
                        "packaged_usage_statistics_ui": (
                            "verified" if packaged_usage_ui_verified
                            else "failed" if any(
                                item.get("error_category") == "packaged_ui"
                                for item in failure_diagnostics
                            )
                            else "unverified"
                        ),
                        "picker_coexistence": "unverified",
                        "resumed_session_switching": "unverified",
                        "explicit_native_opus_5_5_identity": "unverified",
                        "deepseek_chat_messages_usage_ui": "unverified",
                        "deepseek_chat_persisted_usage": (
                            "verified"
                            if any(row.get("case") == "deepseek-chat" for row in usage_rows)
                            else "failed"
                            if any(item.startswith("deepseek-chat:") for item in failures)
                            else "unverified"
                        ),
                        "deepseek_messages_persisted_usage": (
                            "verified"
                            if any(row.get("case") == "claude-deepseek" for row in usage_rows)
                            else "failed"
                            if any(item.startswith("claude-deepseek:") for item in failures)
                            else "unverified"
                        ),
                        "codex_luna_responses_persisted_usage": (
                            "verified"
                            if any(row.get("case") == "responses-luna" for row in usage_rows)
                            else "failed"
                            if any(item.startswith("responses-luna:") for item in failures)
                            else "unverified"
                        ),
                        "codex_luna_responses_usage_ui": "unverified",
                        "tools_compression_cancellation_cache_hit_reuse": "unverified",
                    },
                }
                write_private_evidence(evidence_out, artifact)
                print(
                    f"Saved sanitized E2E evidence ({artifact['status']}): {evidence_out}",
                    flush=True,
                )
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
    parser.add_argument(
        "--packaged-ui",
        action="store_true",
        help=(
            "verify the packaged Usage page; requires dbus-run-session -- xvfb-run and "
            f"{PACKAGED_UI_ISOLATION_ENV}={PACKAGED_UI_ISOLATION_VALUE}"
        ),
    )
    parser.add_argument("--catalog", type=Path,
                        default=home / ".codex" / "model-catalogs" / "codexhub-model-catalog.json")
    parser.add_argument("--case", action="append", choices=(
        "claude-deepseek", "claude-luna", "claude-launcher-deepseek",
        "claude-launcher-luna", "chat-deepseek", "tools-deepseek",
        "responses-deepseek", "responses-luna", "claude-native-haiku",
    ))
    args = parser.parse_args()
    if args.packaged_ui:
        validate_packaged_ui_isolation()
        if args.preflight_only:
            raise SystemExit("--packaged-ui requires a live case and sanitized evidence output")
    selected = set(args.case or ("claude-native-haiku",))
    if args.packaged_ui and "claude-native-haiku" not in selected:
        raise SystemExit("--packaged-ui currently requires the claude-native-haiku case")
    if not 1 <= args.overall_timeout_seconds <= MAX_OVERALL_TIMEOUT_SECONDS:
        raise SystemExit("--overall-timeout-seconds must be between 1 and 600")
    if not 1 <= args.case_timeout_seconds <= MAX_CASE_TIMEOUT_SECONDS:
        raise SystemExit("--case-timeout-seconds must be between 1 and 120")
    if not 1 <= args.max_attempts <= MAX_LIVE_GENERATION_ATTEMPTS:
        raise SystemExit("--max-attempts must be between 1 and 16")
    if not args.candidate_sha or not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
        raise SystemExit("E2E requires --candidate-sha with the built candidate's full SHA")
    if args.resource_root is None:
        raise SystemExit("E2E requires --resource-root set to the portable candidate directory")
    verify_candidate_binding(
        args.bin, args.resource_root, args.source_root, args.candidate_sha,
    )
    if not args.preflight_only:
        if args.claude_bin is None:
            raise SystemExit("Claude Code is not available on PATH; pass --claude-bin")
        if "claude-native-haiku" in selected and args.claude_subscription_source is None:
            raise SystemExit("native Haiku E2E requires --claude-subscription-source")
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
        claude_version=version,
        packaged_ui=args.packaged_ui)


if __name__ == "__main__":
    main()
