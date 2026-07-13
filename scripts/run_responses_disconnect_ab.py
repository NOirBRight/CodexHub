"""Run a sanitized, isolated Responses disconnect A/B with the App-bundled CLI.

The run compares the same short App-server workload across direct official
traffic and isolated Gateway conditions.  Gateway conditions differ by one
variable only: automatic versus process-local explicit Windows proxy routing.
No shared Codex configuration, credentials, system proxy, or user process is
changed.  Required auth/config inputs are copied into temporary homes and are
deleted before this command returns; the emitted report contains aggregates
only.

This is a CLI negative control.  It does not exercise the Codex Desktop App
renderer or its stream-consumer lifecycle, and therefore cannot qualify or
clear a Desktop-App-only disconnect.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    from urllib.request import getproxies_registry
except ImportError:  # pragma: no cover - Windows-only helper.
    getproxies_registry = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src-python"
SCRIPTS_ROOT = REPOSITORY_ROOT / "scripts"
for root in (str(SOURCE_ROOT), str(SCRIPTS_ROOT)):
    if root not in sys.path:
        sys.path.insert(0, root)

import config_overlay
import providers_config
from e2e_codex_catalog_roundtrip import resolve_app_codex


OFFICIAL_MODEL_DEFAULT = "gpt-5.6-terra"
EXTERNAL_MODEL_DEFAULT = "ollama-cloud/glm-5.2"
CONDITIONS = (
    "direct_official",
    "gateway_official_auto",
    "gateway_official_explicit_proxy",
    "gateway_external_auto",
    "gateway_external_explicit_proxy",
)


@dataclass(frozen=True)
class Condition:
    name: str
    model: str
    model_provider: str
    proxy_mode: str | None


@dataclass(frozen=True)
class ConditionResult:
    name: str
    model: str
    model_provider: str
    proxy_mode: str | None
    status: str
    completed_turns: int
    max_duration_seconds: float | None
    slow_turns: int
    cli_exit_code: int | None
    gateway_route_probe: dict[str, bool] | None
    telemetry: dict[str, Any] | None


def _source_codex_home(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex")


def _reserve_loopback_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _registry_proxy_url() -> str | None:
    if not callable(getproxies_registry):
        return None
    try:
        proxies = getproxies_registry()
    except OSError:
        return None
    value = proxies.get("https") or proxies.get("http")
    return str(value) if value else None


def _copy_required_runtime_inputs(source_home: Path, destination_home: Path) -> None:
    """Copy only child-process runtime inputs; never write the source home."""

    destination_home.mkdir(parents=True, exist_ok=True)
    required_inputs = (
        (source_home / "auth.json", destination_home / "auth.json"),
        (source_home / "models_cache.json", destination_home / "models_cache.json"),
        (
            source_home / "model-catalogs" / "codexhub-model-catalog.json",
            destination_home / "model-catalogs" / "codexhub-model-catalog.json",
        ),
        (
            source_home / "proxy" / "config" / "providers.toml",
            destination_home / "proxy" / "config" / "providers.toml",
        ),
    )
    for source, destination in required_inputs:
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _require_runtime_inputs(source_home: Path, *, needs_external: bool) -> None:
    required = [
        source_home / "auth.json",
        source_home / "models_cache.json",
        source_home / "model-catalogs" / "codexhub-model-catalog.json",
    ]
    if needs_external:
        required.append(source_home / "proxy" / "config" / "providers.toml")
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"isolated A/B runtime inputs are unavailable: {', '.join(missing)}")


def _child_environment(
    *,
    codex_home: Path,
    proxy_mode: str,
    registry_proxy: str | None,
    gateway_key: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    environment["CODEX_PROXY_GATEWAY_CLIENT_KEY"] = gateway_key
    # The A/B locates the initiating failure, so Gateway retries are disabled
    # for every Gateway condition.  This is held constant across conditions.
    environment["CODEX_PROXY_AUTO_RETRY_ENABLED"] = "0"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment.pop(name, None)
    if proxy_mode == "explicit_proxy":
        if not registry_proxy:
            raise RuntimeError("an explicit proxy condition requires a configured Windows proxy")
        environment["HTTP_PROXY"] = registry_proxy
        environment["HTTPS_PROXY"] = registry_proxy
    return environment


def _route_probe(environment: dict[str, str]) -> dict[str, bool]:
    """Report proxy selection classes under the exact child environment.

    The values deliberately expose no proxy hostname, port, credential, or
    PAC value.  ``stdlib_https_proxy_selected`` distinguishes the third-party
    urllib path from the Official-specific Windows-registry fallback.
    """

    process = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sys
sys.path.insert(0, r'''%s''')
import codex_proxy
from urllib.request import getproxies
print(json.dumps({
    'explicit_proxy_environment': bool(__import__('os').environ.get('HTTP_PROXY') or __import__('os').environ.get('HTTPS_PROXY')),
    'official_proxy_selected': bool(codex_proxy._official_proxy_url('https://chatgpt.com/backend-api/codex/responses')),
    'stdlib_https_proxy_selected': bool(getproxies().get('https')),
}, sort_keys=True))
"""
            % str(SOURCE_ROOT),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if process.returncode != 0:
        return {
            "explicit_proxy_environment": False,
            "official_proxy_selected": False,
            "stdlib_https_proxy_selected": False,
        }
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        return {
            "explicit_proxy_environment": False,
            "official_proxy_selected": False,
            "stdlib_https_proxy_selected": False,
        }
    return {
        key: bool(payload.get(key))
        for key in (
            "explicit_proxy_environment",
            "official_proxy_selected",
            "stdlib_https_proxy_selected",
        )
    }


def _wait_for_gateway(port: int, timeout_seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError):
            time.sleep(0.1)
    return False


def _stop_gateway(process: subprocess.Popen[str], port: int, gateway_key: str) -> None:
    if process.poll() is None:
        request = Request(
            f"http://127.0.0.1:{port}/shutdown",
            data=b"",
            method="POST",
            headers={"Authorization": f"Bearer {gateway_key}"},
        )
        try:
            with urlopen(request, timeout=2):
                pass
        except (OSError, URLError):
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _summarize_telemetry(events_path: Path) -> dict[str, Any]:
    if not events_path.is_file():
        return {
            "request_start_count": 0,
            "request_terminal_count": 0,
            "unterminated_request_count": 0,
            "upstream_retry_count": 0,
            "failure_groups": [],
            "max_retry_budget": None,
        }

    started: set[str] = set()
    terminal: set[str] = set()
    terminal_events: Counter[str] = Counter()
    failure_groups: Counter[tuple[str, str, str, str, str]] = Counter()
    retry_budgets: list[int] = []
    event_count = 0
    with events_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            event = record.get("event")
            request_id = record.get("request_id")
            if not isinstance(event, str):
                continue
            event_count += 1
            if event == "request_start" and isinstance(request_id, str):
                started.add(request_id)
            if event in {"request_complete", "request_error"}:
                terminal_events[event] += 1
                if isinstance(request_id, str):
                    terminal.add(request_id)
            if event == "upstream_retry":
                budget = record.get("max_attempts")
                if isinstance(budget, int):
                    retry_budgets.append(budget)
            if event in {
                "upstream_retry",
                "request_error",
                "official_passthrough_stream_closed",
                "upstream_stream_interrupted",
                "transparent_stream_closed",
            }:
                failure_groups[
                    (
                        event,
                        str(record.get("failure_phase") or "unknown"),
                        str(record.get("failure_side") or "unknown"),
                        str(record.get("failure_class") or "unknown"),
                        str(record.get("error") or "unknown"),
                    )
                ] += 1
    return {
        "event_count": event_count,
        "request_start_count": len(started),
        "request_terminal_count": len(terminal),
        "unterminated_request_count": len(started - terminal),
        "upstream_retry_count": sum(count for key, count in failure_groups.items() if key[0] == "upstream_retry"),
        "terminal_events": dict(sorted(terminal_events.items())),
        "max_retry_budget": max(retry_budgets) if retry_budgets else None,
        "failure_groups": [
            {
                "event": event,
                "failure_phase": phase,
                "failure_side": side,
                "failure_class": failure_class,
                "error": error,
                "count": count,
            }
            for (event, phase, side, failure_class, error), count in sorted(failure_groups.items())
        ],
    }


def _run_cli_probe(
    *,
    codex_command: Path,
    home: Path,
    model: str,
    model_provider: str,
    turns: int,
    turn_timeout: float,
    pause_between_turns: float,
    input_bytes: int,
) -> tuple[int, int, float | None, int]:
    command = [
        sys.executable,
        str(SCRIPTS_ROOT / "e2e_codex_app_transport.py"),
        "--codex",
        str(codex_command),
        "--home",
        str(home),
        "--cwd",
        str(REPOSITORY_ROOT),
        "--model",
        model,
        "--model-provider",
        model_provider,
        "--turns",
        str(turns),
        "--timeout",
        str(turn_timeout),
        "--pause-between-turns",
        str(pause_between_turns),
        "--input-bytes",
        str(input_bytes),
    ]
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
        timeout=max(60, int(turns * turn_timeout + 45)),
    )
    completed_turns = 0
    max_duration: float | None = None
    slow_turns = 0
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("event") == "turn_completed":
            completed_turns += 1
        if item.get("event") == "probe_completed":
            value = item.get("max_duration_seconds")
            max_duration = float(value) if isinstance(value, (int, float)) else None
            slow = item.get("slow_turns")
            slow_turns = int(slow) if isinstance(slow, int) else 0
    return completed.returncode, completed_turns, max_duration, slow_turns


def _custom_cli_home(
    *,
    source_home: Path,
    destination: Path,
    catalog: Path,
    port: int,
    gateway_key: str,
) -> None:
    _copy_required_runtime_inputs(source_home, destination)
    config_overlay.apply_overlay(
        destination / "config.toml",
        destination / "proxy" / "config.toml.ab.backup",
        catalog,
        f"http://127.0.0.1:{port}",
        owner="beta",
        gateway_key=gateway_key,
    )


def _conditions(official_model: str, external_model: str) -> dict[str, Condition]:
    return {
        "direct_official": Condition("direct_official", official_model, "openai", None),
        "gateway_official_auto": Condition("gateway_official_auto", official_model, "custom", "auto"),
        "gateway_official_explicit_proxy": Condition(
            "gateway_official_explicit_proxy", official_model, "custom", "explicit_proxy"
        ),
        "gateway_external_auto": Condition("gateway_external_auto", external_model, "custom", "auto"),
        "gateway_external_explicit_proxy": Condition(
            "gateway_external_explicit_proxy", external_model, "custom", "explicit_proxy"
        ),
    }


def _run_gateway_condition(
    condition: Condition,
    *,
    source_home: Path,
    codex_command: Path,
    registry_proxy: str | None,
    turns: int,
    turn_timeout: float,
    pause_between_turns: float,
    input_bytes: int,
    scratch: Path,
) -> ConditionResult:
    gateway_home = scratch / "gateway-home"
    cli_home = scratch / "cli-home"
    _copy_required_runtime_inputs(source_home, gateway_home)
    gateway_key = secrets.token_urlsafe(24)
    port = _reserve_loopback_port()
    child_environment = _child_environment(
        codex_home=gateway_home,
        proxy_mode=condition.proxy_mode or "auto",
        registry_proxy=registry_proxy,
        gateway_key=gateway_key,
    )
    route_probe = _route_probe(child_environment)
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(SOURCE_ROOT / "codex_proxy.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=SOURCE_ROOT,
        env=child_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    exit_code: int | None = None
    completed_turns = 0
    maximum: float | None = None
    slow_turns = 0
    gateway_started = False
    try:
        if not _wait_for_gateway(port):
            gateway_started = False
        else:
            gateway_started = True
            catalog = gateway_home / "model-catalogs" / "codexhub-model-catalog.json"
            _custom_cli_home(
                source_home=source_home,
                destination=cli_home,
                catalog=catalog,
                port=port,
                gateway_key=gateway_key,
            )
            exit_code, completed_turns, maximum, slow_turns = _run_cli_probe(
                codex_command=codex_command,
                home=cli_home,
                model=condition.model,
                model_provider=condition.model_provider,
                turns=turns,
                turn_timeout=turn_timeout,
                pause_between_turns=pause_between_turns,
                input_bytes=input_bytes,
            )
    finally:
        _stop_gateway(process, port, gateway_key)
    telemetry = _summarize_telemetry(gateway_home / "proxy" / "codex-proxy-events.jsonl")
    return ConditionResult(
        name=condition.name,
        model=condition.model,
        model_provider=condition.model_provider,
        proxy_mode=condition.proxy_mode,
        status=(
            "gateway_start_failed"
            if not gateway_started
            else "passed"
            if exit_code == 0 and completed_turns == turns
            else "probe_failed"
        ),
        completed_turns=completed_turns,
        max_duration_seconds=maximum,
        slow_turns=slow_turns,
        cli_exit_code=exit_code,
        gateway_route_probe=route_probe,
        telemetry=telemetry,
    )


def _run_direct_condition(
    condition: Condition,
    *,
    source_home: Path,
    codex_command: Path,
    turns: int,
    turn_timeout: float,
    pause_between_turns: float,
    input_bytes: int,
    scratch: Path,
) -> ConditionResult:
    cli_home = scratch / "cli-home"
    _copy_required_runtime_inputs(source_home, cli_home)
    exit_code, completed_turns, maximum, slow_turns = _run_cli_probe(
        codex_command=codex_command,
        home=cli_home,
        model=condition.model,
        model_provider=condition.model_provider,
        turns=turns,
        turn_timeout=turn_timeout,
        pause_between_turns=pause_between_turns,
        input_bytes=input_bytes,
    )
    return ConditionResult(
        name=condition.name,
        model=condition.model,
        model_provider=condition.model_provider,
        proxy_mode=None,
        status="passed" if exit_code == 0 and completed_turns == turns else "probe_failed",
        completed_turns=completed_turns,
        max_duration_seconds=maximum,
        slow_turns=slow_turns,
        cli_exit_code=exit_code,
        gateway_route_probe=None,
        telemetry=None,
    )


def run_ab(
    *,
    source_home: Path,
    codex_command: Path,
    official_model: str,
    external_model: str,
    turns: int,
    turn_timeout: float,
    pause_between_turns: float,
    input_bytes: int,
    selected_conditions: tuple[str, ...],
) -> dict[str, Any]:
    needs_external = any("external" in condition for condition in selected_conditions)
    _require_runtime_inputs(source_home, needs_external=needs_external)
    if needs_external:
        generic_external = providers_config.resolve_external_model_alias(external_model)
        ollama_configured, ollama_external = providers_config.resolve_ollama_cloud_model(external_model)
        if generic_external is None and not (ollama_configured and ollama_external is not None):
            raise ValueError("the selected external model is not configured for this isolated A/B")
    registry_proxy = _registry_proxy_url()
    specifications = _conditions(official_model, external_model)
    results: list[ConditionResult] = []
    with tempfile.TemporaryDirectory(prefix="codexhub-responses-ab-") as temporary_directory:
        scratch_root = Path(temporary_directory)
        for index, condition_name in enumerate(selected_conditions):
            condition = specifications[condition_name]
            condition_scratch = scratch_root / f"condition-{index + 1}"
            if condition.proxy_mode == "explicit_proxy" and registry_proxy is None:
                results.append(
                    ConditionResult(
                        name=condition.name,
                        model=condition.model,
                        model_provider=condition.model_provider,
                        proxy_mode=condition.proxy_mode,
                        status="skipped_no_windows_proxy",
                        completed_turns=0,
                        max_duration_seconds=None,
                        slow_turns=0,
                        cli_exit_code=None,
                        gateway_route_probe=None,
                        telemetry=None,
                    )
                )
            elif condition.proxy_mode is None:
                results.append(
                    _run_direct_condition(
                        condition,
                        source_home=source_home,
                        codex_command=codex_command,
                        turns=turns,
                        turn_timeout=turn_timeout,
                        pause_between_turns=pause_between_turns,
                        input_bytes=input_bytes,
                        scratch=condition_scratch,
                    )
                )
            else:
                results.append(
                    _run_gateway_condition(
                        condition,
                        source_home=source_home,
                        codex_command=codex_command,
                        registry_proxy=registry_proxy,
                        turns=turns,
                        turn_timeout=turn_timeout,
                        pause_between_turns=pause_between_turns,
                        input_bytes=input_bytes,
                        scratch=condition_scratch,
                    )
                )
    return {
        "qualification_surface": {
            "primary_surface": "desktop_app",
            "current_surface": "app_bundled_cli",
            "role": "negative_control_only",
            "desktop_app_root_cause_qualification": "not_supported",
        },
        "workload": {
            "turns": turns,
            "turn_timeout_seconds": turn_timeout,
            "pause_between_turns_seconds": pause_between_turns,
            "synthetic_input_bytes": input_bytes,
            "prompt_shape": "reply exactly OK without tools",
            "gateway_retries_enabled": False,
        },
        "route_environment": {
            "windows_proxy_available": registry_proxy is not None,
            "parent_explicit_proxy_environment": bool(
                os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
            ),
            "direct_official_route": "client_system_default_not_process_introspected",
        },
        "results": [asdict(result) for result in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-codex-home", type=Path)
    parser.add_argument("--codex-command")
    parser.add_argument("--official-model", default=OFFICIAL_MODEL_DEFAULT)
    parser.add_argument("--external-model", default=EXTERNAL_MODEL_DEFAULT)
    parser.add_argument("--turns", type=int, default=5)
    parser.add_argument("--turn-timeout", type=float, default=120.0)
    parser.add_argument("--pause-between-turns", type=float, default=0.0)
    parser.add_argument("--input-bytes", type=int, default=0)
    parser.add_argument("--condition", choices=("all", *CONDITIONS), nargs="+", default=["all"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.turns < 1:
        parser.error("--turns must be positive")
    if args.pause_between_turns < 0:
        parser.error("--pause-between-turns must not be negative")
    if args.input_bytes < 0:
        parser.error("--input-bytes must not be negative")
    selected = CONDITIONS if "all" in args.condition else tuple(args.condition)
    source_home = _source_codex_home(args.source_codex_home)
    codex_command = resolve_app_codex(args.codex_command)
    if args.dry_run:
        result: dict[str, Any] = {
            "qualification_surface": {
                "primary_surface": "desktop_app",
                "current_surface": "app_bundled_cli",
                "role": "negative_control_only",
                "desktop_app_root_cause_qualification": "not_supported",
            },
            "workload": {"turns": args.turns, "prompt_shape": "reply exactly OK without tools"},
            "selected_conditions": selected,
            "runtime_inputs_available": {
                "auth": (source_home / "auth.json").is_file(),
                "models_cache": (source_home / "models_cache.json").is_file(),
                "gateway_catalog": (source_home / "model-catalogs" / "codexhub-model-catalog.json").is_file(),
                "provider_config": (source_home / "proxy" / "config" / "providers.toml").is_file(),
            },
            "app_cli_available": codex_command.is_file(),
            "windows_proxy_available": _registry_proxy_url() is not None,
        }
    else:
        result = run_ab(
            source_home=source_home,
            codex_command=codex_command,
            official_model=args.official_model,
            external_model=args.external_model,
            turns=args.turns,
            turn_timeout=args.turn_timeout,
            pause_between_turns=args.pause_between_turns,
            input_bytes=args.input_bytes,
            selected_conditions=selected,
        )
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
