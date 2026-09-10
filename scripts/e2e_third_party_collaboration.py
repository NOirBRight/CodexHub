#!/usr/bin/env python3
"""Opt-in real Codex + candidate Gateway + third-party Collaboration E2E.

Uses explicitly selected existing local subscription sessions in a private
throwaway home. Reports structural evidence only; never retains credentials,
conversation bodies, or encrypted content. Ordinary pytest never invokes it.
"""
from __future__ import annotations

from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
import ast
from contextlib import ExitStack
import hashlib
from datetime import datetime, timezone
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))
sys.path.insert(0, str(ROOT / "scripts"))
RECORDER_PATH = (ROOT / "scripts" / "e2e_test_recorder.py").resolve()
RECORDER_SHA256 = hashlib.sha256(RECORDER_PATH.read_bytes()).hexdigest()
DEFAULT_PARENT = "xai/grok-4.6"
DEFAULT_REASONING = {
    "xai/grok-4.6": "high",
    "opencode-go/muse-spark-1.3-contributor": "xhigh",
    "commandcode/deepseek/deepseek-v4-flash": "max",
    "commandcode/deepseek/deepseek-v4.1-flash": "max",
    "commandcode/z-ai/glm-5.3-flash": "max",
    "commandcode/meta/muse-spark-1.3-contributor": "max",
    "opencode-go/qwen3.8-flash": "xhigh",
    "opencode-go/hy4-preview": "high",
    "opencode-go/omen-alpha": "high",
}
# The selected CommandCode target is an upstream model identifier, not a
# display-name family.  Keep the match narrow enough that ``v4-flash`` and
# unrelated ``v4.1-*`` variants cannot silently satisfy the 4.1 gate.
COMMANDCODE_DEEPSEEK_41_RE = re.compile(
    r"^deepseek(?:/deepseek)?[-_]v4[._-]1[-_]flash$", re.IGNORECASE
)
SENTINELS = ("E2E_CHILD_OK 323", "E2E_FOLLOWUP_OK 667")
PARENT_SENTINEL = "E2E_PARENT_IMPLEMENTED_OK 941"
NO_SUBAGENT_TURN_SENTINEL = "E2E_NO_SUBAGENT_TURN_OK 818"


class MatrixCase:
    """One real-provider collaboration scenario in the trusted matrix.

    ``repeats`` is the number of independent candidate runs.  The baseline
    is intentionally run once: it is a symptom reference, not evidence that
    the retired implementation is safe to release.
    """

    __slots__ = ("parent_model", "effort", "collaboration_version", "repeats")

    def __init__(self, parent_model: str, effort: str, collaboration_version: str, repeats: int):
        self.parent_model = parent_model
        self.effort = effort
        self.collaboration_version = collaboration_version
        self.repeats = repeats

    @property
    def slug(self) -> str:
        model = re.sub(r"[^A-Za-z0-9]+", "-", self.parent_model).strip("-").lower()
        return f"{model}-{self.collaboration_version}"


def build_trusted_matrix_cases(*, candidate_repeats: int = 3) -> tuple[MatrixCase, ...]:
    """Return the fixed 13-scenario candidate matrix.

    Keep this data in the harness rather than in shell snippets so the report
    can prove exactly which model/protocol/effort combination was requested.
    The CommandCode case is deliberately V2-only and uses the exact confirmed
    DeepSeek V4.1 Flash selector.
    """

    if candidate_repeats < 1:
        raise ValueError("candidate_repeats_must_be_positive")
    return (
        MatrixCase("xai/grok-4.6", "high", "v1", candidate_repeats),
        MatrixCase("xai/grok-4.6", "high", "v2", candidate_repeats),
        MatrixCase(
            "opencode-go/muse-spark-1.3-contributor",
            "xhigh",
            "v1",
            candidate_repeats,
        ),
        MatrixCase(
            "opencode-go/muse-spark-1.3-contributor",
            "xhigh",
            "v2",
            candidate_repeats,
        ),
        MatrixCase("commandcode/deepseek/deepseek-v4.1-flash", "max", "v2", 1),
    )


def build_trusted_baseline_cases() -> tuple[MatrixCase, ...]:
    """Return the four one-shot A/B baseline scenarios."""

    return tuple(
        MatrixCase(case.parent_model, case.effort, case.collaboration_version, 1)
        for case in build_trusted_matrix_cases(candidate_repeats=1)
        if case.parent_model.startswith(("xai/", "opencode-go/"))
    )


def confirm_commandcode_deepseek_41(
    providers_path: Path,
    *,
    selected_model: str = "commandcode/deepseek/deepseek-v4.1-flash",
    requested_effort: str = "max",
) -> dict[str, object]:
    """Confirm the requested CommandCode model without guessing an alias.

    Discovery is intentionally strict and never substitutes the existing
    ``deepseek-v4-flash`` entry.  The returned structure is safe to put in a
    report: it contains only model IDs, explicit capability facts and a
    bounded classification.  A model is confirmed only when the selected
    upstream identifier is present exactly once and the provider explicitly
    advertises the requested reasoning effort.
    """
    from providers_config import discover_provider_models, load_providers

    selected = selected_model.strip()
    selected_upstream = (
        selected[len("commandcode/") :]
        if selected.lower().startswith("commandcode/")
        else selected
    )
    base_result: dict[str, object] = {
        "confirmed": False,
        "classification": None,
        "selected_model": selected,
        "selected_upstream_model": selected_upstream,
        "requested_effort": requested_effort,
        "model_ids": [],
        "reasoning_levels": [],
        "configuration_model_present": False,
        "isolated_model_config_required": False,
    }
    if not selected or not selected_upstream or not COMMANDCODE_DEEPSEEK_41_RE.fullmatch(selected_upstream):
        base_result["classification"] = "selected_model_not_v41_flash"
        return base_result

    providers = list(load_providers(providers_path))
    provider = next((item for item in providers if item.id == "commandcode"), None)
    if provider is None:
        base_result["classification"] = "provider_not_configured"
        return base_result
    api_key = provider.resolved_api_key()
    if not api_key:
        base_result["classification"] = "provider_api_key_unavailable"
        return base_result
    try:
        # This gate proves a *unique* provider fact.  Do not let the general
        # catalog helper collapse duplicate IDs before we decide whether the
        # requested model is unambiguous.
        discovered = discover_provider_models(
            provider.base_url,
            api_key,
            timeout_seconds=20,
            deduplicate=False,
        )
    except TimeoutError:
        base_result["classification"] = "provider_model_list_timeout"
        return base_result
    except HTTPError as exc:
        # Keep the upstream status for diagnosis, never the response body (it
        # may contain provider details or echoed credentials).
        base_result.update({"classification": "provider_model_list_http_error", "status": int(exc.code)})
        return base_result
    except OSError as exc:
        base_result.update({"classification": "provider_model_list_unavailable",
                            "error_type": type(exc).__name__})
        return base_result
    except Exception as exc:  # provider HTTP errors are deliberately typed, not exposed
        base_result.update({"classification": "provider_model_list_error",
                            "error_type": type(exc).__name__})
        return base_result
    records = [
        model for model in discovered
        if isinstance(model, dict) and isinstance(model.get("id"), str)
    ]
    model_ids = sorted(str(model["id"]) for model in records)
    candidates = [model_id for model_id in model_ids if COMMANDCODE_DEEPSEEK_41_RE.fullmatch(model_id)]
    base_result["model_ids"] = candidates
    if len(candidates) != 1:
        base_result["classification"] = "model_id_not_unique"
        return base_result
    if candidates[0] != selected_upstream:
        base_result["classification"] = "selected_model_not_discovered"
        return base_result

    discovered = next(model for model in records if model.get("id") == selected_upstream)
    discovered_levels = tuple(
        level.strip().lower()
        for level in discovered.get("supported_reasoning_levels", ())
        if isinstance(level, str) and level.strip()
    )
    configured = next(
        (
            model for model in provider.models
            if model.id == selected_upstream or selected_upstream in model.aliases
        ),
        None,
    )
    base_result["configuration_model_present"] = configured is not None
    configured_levels = tuple(
        level.strip().lower()
        for level in (configured.supported_reasoning_levels if configured else ())
        if isinstance(level, str) and level.strip()
    )
    # Prefer live endpoint facts.  A configured model is acceptable as the
    # fallback only when the selected ID itself is already declared there;
    # never borrow capabilities from ``deepseek-v4-flash`` or another alias.
    levels = discovered_levels or configured_levels
    base_result["reasoning_levels"] = sorted(set(levels))
    if requested_effort.strip().lower() not in levels:
        base_result["classification"] = "requested_effort_unconfirmed"
        return base_result
    base_result["confirmed"] = True
    base_result["classification"] = "confirmed"
    base_result["isolated_model_config_required"] = configured is None
    metadata = {
        key: discovered[key]
        for key in ("context_window", "max_output_tokens", "supported_reasoning_levels", "default_reasoning_level")
        if key in discovered and discovered[key] is not None
    }
    base_result["metadata"] = metadata
    return base_result


def install_confirmed_commandcode_model(
    source_path: Path,
    target_path: Path,
    confirmation: dict[str, object],
    *,
    collaboration_version: str,
) -> dict[str, object]:
    """Install one *confirmed* upstream model into an isolated config.

    The production providers.toml is never edited.  This helper is called only
    after :func:`confirm_commandcode_deepseek_41` has established the exact
    model ID and requested effort.  It deliberately refuses to construct a
    model from a guessed alias or from the legacy ``deepseek-v4-flash`` entry.
    """

    if confirmation.get("confirmed") is not True:
        raise ValueError("cannot_install_unconfirmed_commandcode_model")
    selected = confirmation.get("selected_upstream_model")
    if not isinstance(selected, str) or not COMMANDCODE_DEEPSEEK_41_RE.fullmatch(selected):
        raise ValueError("confirmed_commandcode_model_id_invalid")
    from providers_config import ModelConfig, load_providers, save_providers

    providers = load_providers(source_path)
    provider = next((item for item in providers if item.id == "commandcode"), None)
    if provider is None:
        raise ValueError("provider_not_configured")
    existing = next((model for model in provider.models if model.id == selected), None)
    metadata = confirmation.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    levels = tuple(
        level.strip().lower()
        for level in confirmation.get("reasoning_levels", [])
        if isinstance(level, str) and level.strip()
    )
    if "max" not in levels:
        raise ValueError("requested_effort_unconfirmed")
    if existing is None:
        context_window = metadata.get("context_window")
        max_output_tokens = metadata.get("max_output_tokens")
        provider.models.append(
            ModelConfig(
                id=selected,
                upstream_model=selected,
                display_name=selected,
                context_window=context_window if isinstance(context_window, int) else None,
                max_output_tokens=max_output_tokens if isinstance(max_output_tokens, int) else None,
                supported_reasoning_levels=tuple(sorted(set(levels))),
                default_reasoning_level=(
                    metadata.get("default_reasoning_level")
                    if isinstance(metadata.get("default_reasoning_level"), str)
                    else "max"
                ),
                thinking_mode="always_on",
                multi_agent_version=collaboration_version,
            )
        )
    else:
        # Do not silently alter an existing declaration.  The exact live model
        # was already selected; only add the missing lifecycle marker to this
        # throwaway copy when it is absent.
        if existing.multi_agent_version is None:
            existing.multi_agent_version = collaboration_version
    save_providers(providers, target_path)
    return {
        "model_id": selected,
        "added": existing is None,
        "target_path": str(target_path),
        "target_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
    }


def inject_confirmed_model_into_catalog(
    catalog_payload: dict[str, object],
    confirmation: dict[str, object],
    *,
    collaboration_version: str,
) -> bool:
    """Add the exact confirmed model to the isolated client catalog.

    Catalogs are client routing metadata, not provider discovery.  A missing
    entry must be added from the already confirmed endpoint facts; no legacy
    model is copied under a new name.
    """

    selected = confirmation.get("selected_model")
    if not isinstance(selected, str) or confirmation.get("confirmed") is not True:
        raise ValueError("cannot_inject_unconfirmed_model")
    models = catalog_payload.get("models")
    if not isinstance(models, list):
        raise ValueError("catalog_models_missing")
    existing = next(
        (model for model in models if isinstance(model, dict) and model.get("slug") == selected),
        None,
    )
    metadata = confirmation.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    entry = existing if isinstance(existing, dict) else {"slug": selected}
    entry.setdefault("display_name", selected)
    entry.setdefault("provider", "commandcode")
    entry["supported_reasoning_levels"] = list(confirmation.get("reasoning_levels", []))
    entry["default_reasoning_level"] = metadata.get("default_reasoning_level", "max")
    entry["multi_agent_version"] = collaboration_version
    for key in ("context_window", "max_output_tokens"):
        if metadata.get(key) is not None:
            entry[key] = metadata[key]
    if existing is None:
        models.append(entry)
        return True
    return False


class RequestObserver:
    """Transparent loopback declaration capture, without retaining bodies/auth."""

    def __init__(self, gateway_port: int):
        self.observations = []
        observations = self.observations

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                observation = None
                try:
                    payload = json.loads(body)
                    tools = payload.get("tools", [])
                    namespaces = sorted(tool["name"] for tool in tools
                                        if isinstance(tool, dict) and tool.get("type") == "namespace")
                    from collaboration_runtime_contract import classify_collaboration_tools
                    version = classify_collaboration_tools(payload.get("tools", [])) if any(
                        name in {"collaboration", "multi_agent_v1"} for name in namespaces
                    ) else None
                    input_items = payload.get("input")
                    observation = {
                        "model": payload.get("model"),
                        "protocol": version,
                        "namespaces": namespaces,
                        "body_sha256": hashlib.sha256(body).hexdigest(),
                        # Keep a structural request fingerprint for provider
                        # 4xx diagnosis; never retain arguments, messages, or
                        # authorization material.
                        "top_level_keys": sorted(
                            key for key in payload if isinstance(key, str)
                        ),
                        "tool_types": sorted(
                            str(tool.get("type")) for tool in tools
                            if isinstance(tool, dict) and isinstance(tool.get("type"), str)
                        ),
                        "tool_count": len(tools) if isinstance(tools, list) else None,
                        "input_count": len(input_items) if isinstance(input_items, list) else None,
                        "input_types": sorted(
                            str(item.get("type")) for item in input_items
                            if isinstance(item, dict) and isinstance(item.get("type"), str)
                        ) if isinstance(input_items, list) else [],
                        "tool_choice_type": type(payload.get("tool_choice")).__name__
                        if "tool_choice" in payload else None,
                    }
                    if len(observations) < 1024:
                        observations.append(observation)
                except (ValueError, TypeError, KeyError):
                    observations.append({"protocol": "unreadable"})
                connection = http.client.HTTPConnection("127.0.0.1", gateway_port, timeout=600)
                try:
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() not in {"host", "connection", "content-length"}}
                    connection.request("POST", self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    if observation is not None and observations and observations[-1] is observation:
                        observation["upstream_status"] = int(response.status)
                        content_type = response.getheader("Content-Type")
                        if isinstance(content_type, str):
                            observation["upstream_content_type"] = content_type.split(";", 1)[0].strip()[:80]
                    response_prefix = bytearray()
                    response_hash = hashlib.sha256()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"connection", "content-length", "transfer-encoding"}:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while chunk := response.read1(65536):
                        response_hash.update(chunk)
                        if len(response_prefix) < 8192:
                            response_prefix.extend(chunk[: 8192 - len(response_prefix)])
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    if observation is not None and observations and observations[-1] is observation:
                        observation["response_body_sha256"] = response_hash.hexdigest()
                        try:
                            error_payload = json.loads(bytes(response_prefix).decode("utf-8"))
                        except (UnicodeDecodeError, ValueError, TypeError):
                            error_payload = None
                        if isinstance(error_payload, dict):
                            observation["response_json_keys"] = sorted(
                                key for key in error_payload if isinstance(key, str)
                            )[:32]
                            safe_error: dict[str, object] = {}
                            for key in ("code", "type", "status", "error"):
                                value = error_payload.get(key)
                                if isinstance(value, (str, int, float, bool)):
                                    text_value = re.sub(
                                        r"(?:sk-|Bearer )[^ \"']+", "<REDACTED>", str(value)
                                    )
                                    safe_error[key] = text_value[:200]
                            if safe_error:
                                observation["response_error_shape"] = safe_error
                except (OSError, http.client.HTTPException):
                    observations.append({"transport_error": True})
                finally:
                    self.close_connection = True
                    connection.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _item_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_item_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_item_text(item) for item in value.values())
    return ""


def _parent_thread_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    direct = value.get("parent_thread_id")
    if isinstance(direct, str) and direct:
        return direct
    for child in value.values():
        parent_id = _parent_thread_id(child)
        if parent_id is not None:
            return parent_id
    return None


def _read_session_records(home: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in (home / "sessions").rglob("*.jsonl"):
        rows: list[dict[str, object]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
        except (OSError, ValueError) as exc:
            raise RuntimeError("session_evidence_unreadable") from exc
        session_id = path.stem
        parent_id = None
        agent_path = None
        model = None
        for row in rows:
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta":
                if isinstance(payload.get("id"), str) and payload["id"]:
                    session_id = payload["id"]
                parent_id = _parent_thread_id(payload.get("source")) or parent_id
                source = payload.get("source")
                if isinstance(source, dict):
                    agent_path = source.get("subagent", {}).get("thread_spawn", {}).get("agent_path")
            if row.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
        records.append(
            {
                "id": session_id,
                "parent_id": parent_id,
                "agent_path": agent_path,
                "model": model,
                "rows": rows,
            }
        )
    return records


def _sentinels_in_records(records: list[dict[str, object]]) -> set[str]:
    found: set[str] = set()
    for record in records:
        for row in record["rows"]:
            if not isinstance(row, dict) or row.get("type") != "response_item":
                continue
            item = row.get("payload")
            if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
                continue
            text = _item_text(item.get("content"))
            found.update(
                sentinel
                for sentinel in (*SENTINELS, PARENT_SENTINEL, NO_SUBAGENT_TURN_SENTINEL)
                if sentinel in text
            )
    return found


def _collaboration_call_names(records: list[dict[str, object]], version: str) -> list[str]:
    namespace = "collaboration" if version == "v2" else "multi_agent_v1"
    calls: list[str] = []
    for record in records:
        for row in record["rows"]:
            if not isinstance(row, dict) or row.get("type") != "response_item":
                continue
            item = row.get("payload")
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call"
                and item.get("namespace") == namespace
                and isinstance(item.get("name"), str)
            ):
                if version != "v2" or item.get("encrypted_function_args") == []:
                    calls.append(item["name"])
    return calls


def _parent_test_tool_call_count(records: list[dict[str, object]]) -> int:
    return _test_tool_call_count(records, "test_parent_task.py")


def _parent_resume_test_tool_call_count(records: list[dict[str, object]]) -> int:
    return _test_tool_call_count(records, "test_resume_turn.py")


def _test_tool_call_count(records: list[dict[str, object]], test_filename: str) -> int:
    return max(
        sum(_successful_test_item(item, test_filename)
            for record in records for item in record.get("cli_items", [])),
        sum(_observed_test_count(observation, test_filename)
            for record in records for observation in record.get("process_observations", [])),
    )


def _observed_test_count(observation: dict, test_filename: str) -> int:
    if not observation.get("available") or any(
        write.get("file") in {"test_parent_task.py", "test_resume_turn.py"}
        for write in observation.get("writes", [])
    ):
        return 0
    return sum(test.get("test") == test_filename and test.get("exit_code") == 0
               and test.get("interpreter_verified") is True and test.get("fixture_open_verified") is True
               for test in observation.get("tests", []))


def _successful_test_item(item: dict, test_filename: str) -> bool:
    return _test_command_diagnostic(item, test_filename)["accepted"]


def _test_command_diagnostic(
    item: dict, test_filename: str, *, require_complete_suite: bool = False
) -> dict:
    """Keep predicate evidence, never arbitrary command arguments or output."""
    expected_files = (
        ("test_parent_task.py", "test_resume_turn.py")
        if require_complete_suite and test_filename == "test_resume_turn.py"
        else (test_filename,)
    )
    argv = []
    reason = None
    wrapper = False
    fixture_cd = False
    try:
        command = item.get("command", "")
        argv = shlex.split(command)
        if len(argv) == 3 and Path(argv[0]).name in {"bash", "sh", "zsh"} and argv[1] in {"-lc", "-c"}:
            wrapper = True
            command = argv[2]
            argv = shlex.split(command)
        fixture = item.get("_fixture_directory")
        # Only literal absolute fixture paths; never evaluate shell syntax.
        if isinstance(fixture, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", fixture):
            for spelling in (fixture, "'" + fixture + "'", '"' + fixture + '"'):
                prefix = "cd " + spelling + " && "
                if command.startswith(prefix):
                    argv = shlex.split(command[len(prefix):])
                    fixture_cd = True
                    break
        # A model may use a harmless environment/export prefix.  Parse it as
        # syntax only; actual test execution still requires the process
        # observer and recorder evidence in real runs.
        if "&&" in argv:
            separator = argv.index("&&")
            prefix_tokens = argv[:separator]
            if prefix_tokens and all(
                token.startswith("export ") or "=" in token or token in {"export"}
                for token in prefix_tokens
            ):
                argv = argv[separator + 1:]
                wrapper = True
    except (ValueError, TypeError):
        reason = "command_parse_error"
    if reason is None and isinstance(command, str) and command.startswith("cd ") and not fixture_cd:
        # Preserve the legacy diagnostic for an unbound working-directory
        # prefix; the fixture-bound form is handled above.
        reason = "argument_count_mismatch"
    interpreters = {sys.executable, os.environ.get("CODEXHUB_E2E_PYTHON"), "$CODEXHUB_E2E_PYTHON"} - {None}
    if item.get("type") != "command_execution":
        reason = "not_command_execution"
    elif item.get("status") != "completed" or item.get("exit_code") != 0:
        reason = "command_not_successful"
    elif reason is None:
        if len(argv) < 4:
            reason = "argument_count_mismatch"
        elif argv[0] not in interpreters:
            reason = "interpreter_mismatch"
        elif argv[1] != "-m" or argv[2] != "unittest":
            reason = "test_arguments_mismatch"
        else:
            arguments = argv[3:]
            test_names = [argument for argument in arguments if argument.endswith(".py")]
            invalid = {
                "-k", "--locals", "--buffer", "discover", "-s", "-p", "-t",
            }
            if any(argument in invalid or argument.startswith("-") and argument not in {"-q", "-v"}
                   for argument in arguments):
                reason = "test_arguments_mismatch"
            elif tuple(test_names) != expected_files or len(arguments) != len(test_names) + sum(
                argument in {"-q", "-v"} for argument in arguments
            ):
                reason = "test_arguments_mismatch"
    safe_tokens = {"-m", "unittest", "-q", "-v", "test_parent_task.py", "test_resume_turn.py",
                   "&&", ";", "cd", "echo", "python", "python3"}
    return {
        "item_id": item.get("id"), "status": item.get("status"), "exit_code": item.get("exit_code"),
        "expected_test": test_filename, "accepted": reason is None, "reason": reason,
        "shell_wrapper": wrapper, "fixture_cd_verified": fixture_cd,
        "argv_shape": ["<BOUND_PYTHON>" if arg in interpreters else arg if arg in safe_tokens
                       else "<REDACTED>" for arg in argv],
    }


def _timestamp_value(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _process_test_is_trusted(observation: dict, test_filename: str) -> bool:
    """Require process and recorder evidence when the observer was enabled."""
    if not observation.get("available"):
        # Synthetic unit fixtures created by this module predate the process
        # observer.  Real runs enable it by default and therefore take the
        # strict branch below.
        return False
    process_ok = _observed_test_count(observation, test_filename) > 0
    if process_ok and "test_evidence" not in observation:
        # Compatibility for pre-v8 in-memory fixtures.  The real harness now
        # always emits the recorder key and takes the strict branch.
        return True
    recorder = observation.get("test_evidence")
    if not (process_ok and isinstance(recorder, dict) and recorder.get("complete") is True):
        return False
    # The recorder is a fixed harness module, not a fixture-provided helper.
    # Both its self-reported source identity and the kernel trace opening that
    # module must be present before a real test can prove execution.
    if observation.get("recorder_open_verified") is not True:
        return False
    if observation.get("evidence_write_verified") is not True:
        return False
    # The test process must be in the same strace-observed tree as the Codex
    # client that produced this turn.  A standalone successful unittest is not
    # evidence that the parent agent actually ran it.
    if observation.get("client_tree_verified") is not True:
        return False
    if recorder.get("recorder_path") != str(RECORDER_PATH):
        return False
    if recorder.get("recorder_sha256") != RECORDER_SHA256:
        return False
    # ``_run_client`` snapshots the fixture immediately after the client turn.
    # If the model edits the source or test after running unittest, the
    # recorder's end digest no longer covers the final workspace version and
    # this turn is unverified rather than falsely green.
    if (
        observation.get("source_sha_after") is not None
        and recorder.get("source_sha_at_test_finish") != observation.get("source_sha_after")
    ):
        return False
    if (
        observation.get("test_sha_after") is not None
        and recorder.get("test_sha_at_test_finish") != observation.get("test_sha_after")
    ):
        return False
    # A fixture may execute inside a PID namespace (the recorder then reports
    # PID 2 while host-side strace sees the corresponding process as e.g.
    # 1741063).  Correlate the two observations by the exact interpreter,
    # fixture file opens, exit status, and the recorder's execution interval;
    # do not require namespace-local PID numbers to be identical.
    suite_start = _timestamp_value(recorder.get("suite_started_at"))
    suite_end = _timestamp_value(recorder.get("suite_finished_at"))
    if suite_start is None or suite_end is None:
        return False
    for write in observation.get("writes", []):
        if not isinstance(write, dict) or write.get("file") not in {
            "parent_task.py", test_filename,
        }:
            continue
        timestamp = write.get("timestamp")
        if isinstance(timestamp, (int, float)) and suite_start <= timestamp <= suite_end:
            return False
    evidence_pids = {
        event.get("pid") for event in recorder.get("events", [])
        if isinstance(event, dict) and isinstance(event.get("pid"), int)
    }
    if not evidence_pids or len(evidence_pids) != 1:
        return False
    for process in observation.get("processes", []):
        if not isinstance(process, dict) or process.get("exit_code") != 0:
            continue
        if test_filename not in process.get("test_files", []):
            continue
        started = process.get("started_at")
        finished = process.get("finished_at")
        if (
            isinstance(started, (int, float))
            and isinstance(finished, (int, float))
            and started <= suite_start <= suite_end <= finished
        ):
            return True
    return False


def _test_evidence_order(
    observation: dict, parent: dict, child: dict, parent_turn: dict | None = None
) -> dict:
    recorder = observation.get("test_evidence") if isinstance(observation, dict) else {}
    first_review = [
        _timestamp_value(row.get("timestamp"))
        for row in child.get("rows", [])
        if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "task_complete"
    ]
    review_end = max((value for value in first_review if value is not None), default=None)
    edit_times = [
        _timestamp_value(row.get("timestamp"))
        for row in parent.get("rows", [])
        if row.get("type") == "response_item"
        and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
        and row.get("payload", {}).get("name") == "apply_patch"
    ]
    process_writes = observation.get("writes", []) if isinstance(observation, dict) else []
    edit_times.extend(
        float(write["timestamp"])
        for write in process_writes
        if write.get("file") == "parent_task.py" and isinstance(write.get("timestamp"), (int, float))
    )
    edit = min(edit_times) if edit_times else None
    suite_start = _timestamp_value(recorder.get("suite_started_at")) if isinstance(recorder, dict) else None
    suite_end = _timestamp_value(recorder.get("suite_finished_at")) if isinstance(recorder, dict) else None
    parent_end = _timestamp_value((parent_turn or parent).get("terminal_timestamp"))
    review_required = bool(child.get("rows"))
    values_present = all(value is not None for value in (edit, suite_start, suite_end, parent_end))
    ordered = values_present and suite_start <= suite_end <= parent_end and (
        not review_required or (review_end is not None and review_end <= edit <= suite_start)
    )
    return {
        "review_end": review_end, "parent_edit": edit,
        "test_started": suite_start, "test_finished": suite_end,
        "parent_turn_completed": parent_end, "ordered": ordered,
        "rejection": None if ordered else "execution_order_missing_or_invalid",
    }


def _read_client_turn(path: Path) -> dict:
    """CLI events are execution evidence; rollout prompt text is not."""
    result = {
        "thread_id": None, "terminal": None, "items": [],
        "started_at": None, "completed_at": None, "turn_id": None,
        "terminal_error": None,
    }
    started, completed = set(), set()
    started_at: dict[str, object] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            # CLI stderr warnings may be interleaved, but a broken event may
            # not be discarded as a warning.
            if line.lstrip().startswith("{"):
                raise RuntimeError("client_event_unreadable")
            continue
        kind = event.get("type")
        if kind == "thread.started":
            if result["thread_id"] is not None:
                raise RuntimeError("duplicate_client_thread")
            result["thread_id"] = event.get("thread_id")
            result["started_at"] = event.get("timestamp")
            result["turn_id"] = event.get("turn_id")
        elif kind in {"turn.completed", "turn.failed"}:
            if result["terminal"] is not None:
                raise RuntimeError("duplicate_client_terminal")
            result["terminal"] = kind
            result["completed_at"] = event.get("timestamp")
            result["turn_id"] = event.get("turn_id") or result.get("turn_id")
            if kind == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict):
                    # Keep only bounded structural details.  The raw client
                    # stream can contain provider text or credentials.
                    safe: dict[str, object] = {}
                    for key in ("code", "type", "status", "reason", "message"):
                        value = error.get(key)
                        if isinstance(value, (str, int, float, bool)):
                            text_value = str(value)
                            text_value = re.sub(r"(?:sk-|Bearer )[^ \"']+", "<REDACTED>", text_value)
                            text_value = re.sub(r"/(?:home|tmp)/[^\s\"']+", "<PATH>", text_value)
                            safe[key] = text_value[:1000]
                    result["terminal_error"] = safe
                elif isinstance(error, str):
                    result["terminal_error"] = {"message": re.sub(
                        r"(?:sk-|Bearer )[^ \"']+", "<REDACTED>", error
                    )[:1000]}
        elif kind in {"item.started", "item.completed"}:
            item = event.get("item", {})
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise RuntimeError("missing_client_item_id")
            if kind == "item.started":
                if item_id in started or item_id in completed:
                    raise RuntimeError("duplicate_client_item")
                started.add(item_id)
                started_at[item_id] = event.get("timestamp")
            else:
                if item_id in completed:
                    raise RuntimeError("duplicate_client_item")
                if item.get("type") == "command_execution" and item_id not in started:
                    raise RuntimeError("missing_command_start")
                completed.add(item_id)
                item["_event_timestamp"] = event.get("timestamp")
                item["_started_timestamp"] = started_at.get(item_id)
                result["items"].append(item)
    if started - completed:
        result["terminal"] = "incomplete_items"
    return result


def _turns(record: dict) -> list[dict]:
    turns = []
    for row in record["rows"]:
        item = row.get("payload", {})
        if row.get("type") == "event_msg" and item.get("type") == "task_started":
            turns.append({"id": item.get("turn_id"), "rows": [], "terminal": None,
                          "terminal_timestamp": None})
        if turns:
            turns[-1]["rows"].append(row)
            if row.get("type") == "event_msg" and item.get("type") in {"task_complete", "turn_aborted"}:
                if turns[-1]["terminal"] is not None or item.get("turn_id") != turns[-1]["id"]:
                    raise RuntimeError("ambiguous_rollout_terminal")
                turns[-1]["terminal"] = item["type"]
                turns[-1]["terminal_timestamp"] = row.get("timestamp")
    return turns


def _paired_collaboration_calls(record: dict, version: str) -> list[dict]:
    namespace = "collaboration" if version == "v2" else "multi_agent_v1"
    calls, results = {}, {}
    for row in record["rows"]:
        item = row.get("payload", {})
        if row.get("type") != "response_item":
            continue
        call_id = item.get("call_id")
        if item.get("type") == "function_call" and item.get("namespace") == namespace:
            if not isinstance(call_id, str) or not call_id or call_id in calls:
                raise RuntimeError("ambiguous_collaboration_call")
            try:
                arguments = json.loads(item.get("arguments", ""))
            except (ValueError, TypeError) as exc:
                raise RuntimeError("unreadable_collaboration_arguments") from exc
            calls[call_id] = {"id": call_id, "name": item.get("name"), "arguments": arguments,
                              "timestamp": row.get("timestamp")}
        elif item.get("type") == "function_call_output" and call_id in calls:
            if call_id in results:
                raise RuntimeError("duplicate_collaboration_result")
            output = item.get("output")
            try:
                output = json.loads(output) if isinstance(output, str) else output
            except ValueError:
                pass  # Native V2 send/followup acknowledgements can be empty.
            results[call_id] = output
    if set(calls) != set(results):
        raise RuntimeError("missing_collaboration_result")
    for call_id, call in calls.items():
        call["output"] = results[call_id]
        output = call["output"]
        call["failed"] = (
            isinstance(output, dict) and bool(output.get("error") or output.get("isError"))
        ) or (
            isinstance(output, str) and output.startswith("failed to parse function arguments:")
        )
        identity_key = "task_name" if version == "v2" else "agent_id"
        call["creation_verified"] = (
            call["name"] == "spawn_agent" and not call["failed"]
            and isinstance(output, dict) and isinstance(output.get(identity_key), str)
            and bool(output[identity_key])
        )
    return list(calls.values())


def _child_inspect_only(record: dict) -> bool:
    """Require successful, paired read-only commands, not a sandbox label.

    The fixture prompt deliberately limits inspection to cat. Unknown shell
    syntax is unverified rather than assumed harmless. No shell interpreter,
    substitutions, redirections, or model-authored test code are accepted.
    """
    calls, results = {}, {}
    for row in record["rows"]:
        item = row.get("payload", {})
        if row.get("type") != "response_item":
            continue
        if item.get("type") in {"function_call", "custom_tool_call"}:
            if item.get("name") not in {"exec_command", "shell_command"} or item.get("namespace") not in {None, "functions"}:
                return False
            try:
                args = json.loads(item.get("arguments", ""))
                argv = shlex.split(args.get("cmd", args.get("command", "")))
            except (ValueError, TypeError, AttributeError):
                return False
            if len(argv) < 2 or argv[0] != "cat" or any(
                arg not in {"parent_task.py", "child_review.txt"} for arg in argv[1:]
            ):
                return False
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id in calls:
                return False
            calls[call_id] = item
        elif item.get("type") == "function_call_output" and item.get("call_id") in calls:
            call_id = item["call_id"]
            if call_id in results:
                return False
            output = item.get("output")
            results[call_id] = (isinstance(output, dict) and output.get("exit_code") == 0) or (
                isinstance(output, str) and bool(re.search(r"(?:^|\n)(?:Process exited with code 0|Exit code: 0)(?:\n|$)", output))
            )
    return bool(calls) and set(calls) == set(results) and all(results.values())


def _lifecycle_passed(parent: dict, child: dict, version: str, diagnostics: dict | None = None) -> tuple[bool, list[dict]]:
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics["rejected_steps"] = []
    calls = _paired_collaboration_calls(parent, version)
    def reject(reason):
        diagnostics["failure"] = reason
        return False, calls
    successful = [call for call in calls if not call["failed"]
                  and (call["name"] != "spawn_agent" or call["creation_verified"])]
    spawns = [call for call in successful if call["name"] == "spawn_agent"]
    if len(spawns) != 1 or not isinstance(spawns[0]["output"], dict):
        return reject("successful_spawn_count_or_result_invalid")
    spawn = spawns[0]
    output = spawn["output"]
    identity = output.get("task_name") if version == "v2" else output.get("agent_id")
    if not isinstance(identity, str) or not identity or identity != (child.get("agent_path") if version == "v2" else child["id"]):
        return reject("spawn_child_identity_mismatch")
    completions_for_diagnostics = [row for row in child["rows"] if row.get("type") == "event_msg"
                                  and row.get("payload", {}).get("type") == "task_complete"]
    messages_for_diagnostics = [row["payload"].get("last_agent_message", "") for row in completions_for_diagnostics]
    diagnostics["completion_records"] = [
        {"timestamp": row.get("timestamp"), "turn_id": row["payload"].get("turn_id")}
        for row in completions_for_diagnostics]
    def native_summary(value):
        if isinstance(value, dict):
            return {str(key): native_summary(item) for key, item in value.items()}
        if isinstance(value, list):
            return [native_summary(item) for item in value[:8]]
        if isinstance(value, str):
            # Synthetic lifecycle responses only. Keep error semantics without
            # retaining review bodies, paths, or opaque provider tokens.
            if any(word in value.lower() for word in ("error", "invalid", "missing", "unknown", "expected")):
                clean = re.sub(r"(?:sk-|Bearer )[^\s\"']+", "<REDACTED>", value)
                clean = re.sub(r"/(?:home|tmp)/[^\s\"']+", "<PATH>", clean)
                return clean[:1200]
            return {"text_length": len(value)}
        return value
    def contains_message(value, message):
        if isinstance(value, str):
            return bool(message) and message in value
        if isinstance(value, dict):
            return any(contains_message(item, message) for item in value.values())
        if isinstance(value, list):
            return any(contains_message(item, message) for item in value)
        return False
    diagnostics["native_results"] = []
    for call in calls:
        result = call["output"] if isinstance(call["output"], dict) else {}
        status = result.get("status", {})
        target_status = status.get(identity, {}) if isinstance(status, dict) else {}
        previous = result.get("previous_status", {})
        diagnostics["native_results"].append({
            "call_id": call["id"], "name": call["name"], "timestamp": call.get("timestamp"),
            "timed_out": result.get("timed_out") if isinstance(result.get("timed_out"), bool) else None,
            "target_completed": isinstance(target_status, dict) and "completed" in target_status,
            "previous_completed": isinstance(previous, dict) and "completed" in previous,
            "result_shape": native_summary(call["output"]),
            "child_message_matches": [contains_message(call["output"], message)
                                      for message in messages_for_diagnostics],
        })
    diagnostics["delivery_candidates"] = []
    for row in parent["rows"]:
        item = row.get("payload", {})
        if row.get("type") != "response_item" or item.get("type") not in {"message", "agent_message"}:
            continue
        content = _item_text(item.get("content"))
        matches = [bool(message) and message in content for message in messages_for_diagnostics]
        if any(matches):
            diagnostics["delivery_candidates"].append({
                "timestamp": row.get("timestamp"), "type": item.get("type"), "role": item.get("role"),
                "identity_present": identity in content, "child_message_matches": matches,
                "native_notification_marker": "<subagent_notification>" in content,
            })
    targets = {identity}
    if version == "v2" and isinstance(spawn["arguments"].get("task_name"), str):
        targets.add(spawn["arguments"]["task_name"])
    steps = ["spawn_agent", "followup_task"] if version == "v2" else [
        "spawn_agent", "close_agent", "resume_agent", "send_input", "close_agent",
    ]
    position = 0
    matched_calls = []
    for call in successful:
        if position == len(steps) or call["name"] != steps[position]:
            continue
        args = call["arguments"]
        if position and version == "v2" and args.get("target") not in targets:
            diagnostics["rejected_steps"].append({"call_id": call["id"], "name": call["name"], "reason": "target_identity_mismatch"})
            continue
        if position and version == "v1":
            ids = (args.get("targets") if call["name"] == "wait_agent"
                   else [args.get("id" if call["name"] == "resume_agent" else "target")])
            if ids != [identity]:
                diagnostics["rejected_steps"].append({"call_id": call["id"], "name": call["name"], "reason": "target_identity_mismatch"})
                continue
            if call["name"] == "wait_agent":
                status = call["output"].get("status", {}) if isinstance(call["output"], dict) else {}
                if not isinstance(status.get(identity), dict) or "completed" not in status[identity]:
                    diagnostics["rejected_steps"].append({"call_id": call["id"], "name": call["name"], "reason": "wait_has_no_completed_child_result"})
                    continue
        matched_calls.append(call)
        position += 1
    child_turns = _turns(child)
    diagnostics["matched_steps"] = [call["name"] for call in matched_calls]
    diagnostics["next_expected_step"] = steps[position] if position < len(steps) else None
    diagnostics["child_turns"] = [{"id": turn["id"], "terminal": turn["terminal"]} for turn in child_turns]
    if position != len(steps):
        return reject("native_sequence_incomplete")
    lifecycle = position == len(steps) and len(child_turns) == 2 and all(
        turn["terminal"] == "task_complete" for turn in child_turns
    )
    completions = [row for row in child["rows"] if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "task_complete"]
    if not lifecycle or len(completions) != 2:
        return reject("child_completion_count_or_terminal_invalid")
    messages = [row["payload"].get("last_agent_message") for row in completions]
    if not all(isinstance(message, str) and message.strip() for message in messages):
        return reject("child_completion_message_missing")
    followup = next(call for call in matched_calls if call["name"] == ("followup_task" if version == "v2" else "send_input"))
    first_done, second_done = [row.get("timestamp") for row in completions]
    diagnostics["completion_timing"] = {"first_done": first_done, "followup": followup.get("timestamp"), "second_done": second_done}
    if not all(isinstance(value, str) for value in (first_done, second_done, followup.get("timestamp"))) or not (
        first_done <= followup["timestamp"] <= second_done
    ):
        return reject("followup_completion_order_invalid")
    if version == "v1":
        closes = [call for call in matched_calls if call["name"] == "close_agent"]
        for index, (done, close) in enumerate(zip((first_done, second_done), closes)):
            if not isinstance(close.get("timestamp"), str) or close["timestamp"] < done:
                return reject("close_before_child_completion")
            prior_status = close["output"].get("previous_status", {}) if isinstance(close["output"], dict) else {}
            close_delivered = isinstance(prior_status, dict) and prior_status.get("completed") == messages[index]
            wait_delivered = any(
                call["name"] == "wait_agent" and call["arguments"].get("targets") == [identity]
                and isinstance(call["output"], dict)
                and isinstance(call["output"].get("status"), dict)
                and isinstance(call["output"]["status"].get(identity), dict)
                and call["output"]["status"][identity].get("completed") == messages[index]
                and call.get("timestamp", "") <= close["timestamp"]
                and (index == 0 or call.get("timestamp", "") >= followup["timestamp"])
                for call in successful)
            if not (close_delivered or wait_delivered):
                return reject("child_result_delivery_not_correlated")
    if version == "v2":
        delivered = [row.get("payload", {}) for row in parent["rows"]
                     if row.get("type") == "response_item" and row.get("payload", {}).get("type") == "agent_message"
                     and row["payload"].get("author") == identity
                     and row["payload"].get("recipient") == identity.rsplit("/", 1)[0]]
        strict_delivery = all(
            any(message in _item_text(item.get("content")) for item in delivered)
            for message in messages
        )
        # Some Codex CLI/provider combinations preserve the real child
        # identity in the delivered text but omit author/recipient metadata
        # on the replayed ``agent_message`` item.  The child terminal records
        # and identity-bearing content are still an unambiguous correlation;
        # accept that protocol shape without accepting plain user text.
        observed_delivery = diagnostics.get("delivery_candidates", [])
        text_delivery = all(
            any(
                candidate.get("identity_present") is True
                and isinstance(candidate.get("child_message_matches"), list)
                and index < len(candidate["child_message_matches"])
                and candidate["child_message_matches"][index] is True
                for candidate in observed_delivery
            )
            for index in range(len(messages))
        )
        if not strict_delivery and not text_delivery:
            return reject("child_result_delivery_not_correlated")
    diagnostics["failure"] = None
    return True, calls


def _parent_patch_after_reviews(parent: dict, child: dict) -> bool:
    # Correlate the parent edit request/result with the CLI file-change event.
    # Without the child's completed review before that edit, completion text
    # alone would wrongly accept a parent that bypassed the review altogether.
    completions = [row for row in child["rows"] if row.get("type") == "event_msg"
                   and row.get("payload", {}).get("type") == "task_complete"]
    if not completions or not isinstance(completions[-1].get("timestamp"), str):
        return False
    last_review = completions[-1]["timestamp"]
    if any(row.get("timestamp", "") < last_review for row in parent["rows"]
           if row.get("type") == "response_item" and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
           and row["payload"].get("name") == "apply_patch"):
        return False
    patch_ids = {row["payload"].get("call_id") for row in parent["rows"]
                 if row.get("type") == "response_item" and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
                 and row["payload"].get("name") == "apply_patch"
                 and isinstance(last_review, str) and row.get("timestamp", "") >= last_review}
    patch_results = {row["payload"].get("call_id") for row in parent["rows"]
                     if row.get("type") == "response_item" and row.get("payload", {}).get("type") in {"custom_tool_call_output", "function_call_output"}}
    return bool((patch_ids - {None}) & patch_results)


def _parent_session_id(home: Path, parent_thread_id: str | None) -> str | None:
    """Return the one real parent associated with a spawned child, if known.

    ``--last`` is unsafe here because a child may be the most recent session.
    The first client's ``thread.started.thread_id`` and the recorded child
    relationship are the only authorities for selecting the session to
    resume.  Model names are deliberately not consulted: parent and child are
    normally configured with the same model and a model label cannot identify
    a lifecycle owner.
    """
    if not isinstance(parent_thread_id, str) or not parent_thread_id:
        return None
    records = _read_session_records(home)
    child_parent_ids = {
        record["parent_id"]
        for record in records
        if isinstance(record.get("parent_id"), str)
    }
    candidates = [
        record["id"]
        for record in records
        if record.get("id") == parent_thread_id and record.get("id") in child_parent_ids
    ]
    return candidates[0] if len(candidates) == 1 and isinstance(candidates[0], str) else None


def collect_evidence(
    home: Path,
    child_model: str,
    collaboration_version: str = "v2",
    *,
    parent_model: str | None = None,
    parent_effort: str | None = None,
    child_effort: str | None = None,
    client_outputs: tuple[Path, ...] = (),
    fixture_directory: Path | None = None,
    require_process_evidence: bool = False,
) -> dict:
    """Collect structural evidence without retaining prompts or session contents."""
    parent_model = parent_model or child_model
    child_results: set[str] = set()
    parent_results: set[str] = set()
    plaintext_handoffs = 0
    encrypted_handoffs = 0
    records = _read_session_records(home)
    records_by_id = {record["id"]: record for record in records if isinstance(record["id"], str)}
    if len(records_by_id) != len(records):
        raise RuntimeError("duplicate_session_identity")
    child_records = [record for record in records if isinstance(record.get("parent_id"), str)]
    parent_ids = {record["parent_id"] for record in child_records}
    parent_records = [records_by_id[parent_id] for parent_id in parent_ids if parent_id in records_by_id]
    relationships = sorted(
        {
            (record["id"], record["parent_id"])
            for record in child_records
            if isinstance(record["id"], str) and isinstance(record["parent_id"], str)
        }
    )
    child_models = sorted(
        {record["model"] for record in child_records if isinstance(record.get("model"), str)}
    )
    parent_models = sorted(
        {record["model"] for record in parent_records if isinstance(record.get("model"), str)}
    )
    client_turns = [_read_client_turn(path) for path in client_outputs]
    for turn, path in zip(client_turns, client_outputs):
        observation_path = Path(str(path) + ".process.json")
        turn["process_observation"] = json.loads(observation_path.read_text()) if observation_path.is_file() else {}
        for item in turn["items"]:
            # Overwrite any client-supplied value with harness-owned context.
            item["_fixture_directory"] = str(fixture_directory.resolve()) if fixture_directory else None
    for turn in client_turns:
        parent = records_by_id.get(turn["thread_id"])
        if parent is None or parent.get("parent_id") is not None:
            raise RuntimeError("client_parent_relationship_mismatch")
        if parent not in parent_records:
            parent_records.append(parent)
        parent.setdefault("cli_items", []).extend(turn["items"])
        parent.setdefault("process_observations", []).append(turn["process_observation"])
    if len({turn["thread_id"] for turn in client_turns}) > 1:
        raise RuntimeError("multiple_client_parents")
    parent_models = sorted({record["model"] for record in parent_records if isinstance(record.get("model"), str)})
    for record in child_records:
        for row in record["rows"]:
            item = row.get("payload", {})
            if not isinstance(item, dict) or row.get("type") != "response_item":
                continue
            if item.get("type") == "agent_message":
                parts = item.get("content", [])
                if isinstance(parts, list):
                    encrypted_handoffs += sum(isinstance(part, dict) and part.get("type") == "encrypted_content" for part in parts)
                    plaintext_handoffs += sum(isinstance(part, dict) and part.get("type") == "input_text" for part in parts)
            if item.get("type") == "message" and item.get("role") == "assistant":
                text = _item_text(item.get("content"))
                child_results.update(sentinel for sentinel in SENTINELS if sentinel in text)
    parent_results = _sentinels_in_records(parent_records)
    call_names = _collaboration_call_names(parent_records, collaboration_version)
    portable_calls = set(call_names)
    handoff_contract_passed = (
        plaintext_handoffs >= 2 and encrypted_handoffs == 0
        if collaboration_version == "v2"
        # V1 has no V2 agent_message lifecycle. Its child/parent association
        # is demonstrated by the client thread relationship and paired native
        # calls, so an absent V2 handoff is expected rather than a failure.
        else plaintext_handoffs == 0 and encrypted_handoffs == 0
    )
    lifecycle_passed, calls = (False, [])
    lifecycle_diagnostics = {"failure": "parent_or_child_relationship_missing"}
    if len(parent_records) == 1:
        calls = _paired_collaboration_calls(parent_records[0], collaboration_version)
        if len(child_records) == 1:
            lifecycle_passed, calls = _lifecycle_passed(parent_records[0], child_records[0], collaboration_version, lifecycle_diagnostics)
    contexts_match = True
    for group, model, effort in ((parent_records, parent_model, parent_effort), (child_records, child_model, child_effort)):
        for record in group:
            contexts = [row["payload"] for row in record["rows"] if row.get("type") == "turn_context"]
            contexts_match &= bool(contexts) and all(
                context.get("model") == model
                and (effort is not None and context.get("effort") == effort)
                and context.get("multi_agent_version") == collaboration_version
                for context in contexts
            )
    parent_turns = _turns(parent_records[0]) if len(parent_records) == 1 else []
    no_subagent_turn = len(parent_turns) <= 1 or not any(
        _collaboration_call_names([parent_turns[1]], version) for version in ("v1", "v2")
    )
    test_diagnostics: list[dict[str, object]] = []
    tests_and_terminals_verified = bool(client_turns) and len(client_turns) == len(parent_turns)
    child_turns = _turns(child_records[0]) if len(child_records) == 1 else []
    if tests_and_terminals_verified:
        for index, turn in enumerate(client_turns):
            test_filename = "test_parent_task.py" if index == 0 else "test_resume_turn.py"
            observation = turn.get("process_observation", {})
            command_items = [item for item in turn["items"]
                             if item.get("type") == "command_execution"
                             and _test_command_diagnostic(
                                 item, test_filename,
                                 require_complete_suite=require_process_evidence,
                             ).get("accepted")]
            process_verified = _process_test_is_trusted(observation, test_filename)
            if "test_evidence" not in observation and command_items:
                process_verified = True
            legacy_verified = any(_successful_test_item(item, test_filename) for item in command_items)
            # Process/recorder evidence is authoritative for actual runs.  A
            # synthetic in-memory fixture without a process sidecar remains
            # useful to unit tests but is explicitly marked legacy below.
            test_ok = process_verified if observation.get("available") else legacy_verified
            order = _test_evidence_order(
                observation,
                parent_turns[index],
                child_turns[0] if index == 0 and child_turns else {"rows": []},
                parent_turns[index],
            ) if child_records else {}
            order_ok = (
                not require_process_evidence
                or not observation.get("available")
                or order.get("ordered") is True
            )
            recorder_evidence = observation.get("test_evidence", {}) or {}
            suite_start = _timestamp_value(recorder_evidence.get("suite_started_at"))
            suite_end = _timestamp_value(recorder_evidence.get("suite_finished_at"))
            writes_during_test = any(
                isinstance(write, dict)
                and write.get("file") in {"parent_task.py", test_filename}
                and isinstance(write.get("timestamp"), (int, float))
                and suite_start is not None
                and suite_end is not None
                and suite_start <= write["timestamp"] <= suite_end
                for write in observation.get("writes", [])
            )
            test_diagnostics.append({
                "turn_index": index, "test": test_filename,
                "expected_cases": observation.get("expected_cases", []),
                "verified_cases": (observation.get("test_evidence", {}) or {}).get("verified_cases", []),
                "process_verified": process_verified,
                "command_shape_verified": bool(command_items),
                "legacy_structural_fallback": not observation.get("available"),
                "source_sha_before": observation.get("source_sha_before"),
                "source_sha_after": observation.get("source_sha_after"),
                "test_sha_before": observation.get("test_sha_before"),
                "test_sha_after": observation.get("test_sha_after"),
                "source_unchanged_during_test": (
                    recorder_evidence.get("source_sha_at_test_start") is not None
                    and recorder_evidence.get("source_sha_at_test_start")
                    == recorder_evidence.get("source_sha_at_test_finish")
                    and recorder_evidence.get("source_sha_at_test_finish")
                    == observation.get("source_sha_after")
                    and not writes_during_test
                ) if observation.get("test_evidence") else (
                    observation.get("source_sha_before") is not None
                    and observation.get("source_sha_before") == observation.get("source_sha_after")
                    and observation.get("source_changed") is not True
                ) if observation.get("available") else None,
                "test_unchanged_during_test": (
                    recorder_evidence.get("test_sha_at_test_start") is not None
                    and recorder_evidence.get("test_sha_at_test_start")
                    == recorder_evidence.get("test_sha_at_test_finish")
                    and recorder_evidence.get("test_sha_at_test_finish")
                    == observation.get("test_sha_after")
                    and not writes_during_test
                ) if observation.get("test_evidence") else (
                    observation.get("test_sha_before") is not None
                    and observation.get("test_sha_before") == observation.get("test_sha_after")
                    and observation.get("test_changed") is not True
                ) if observation.get("available") else None,
                "passed": turn["terminal"] == "turn.completed"
                and parent_turns[index]["terminal"] == "task_complete"
                and test_ok
                and order_ok,
                "execution_order": order,
            })
            tests_and_terminals_verified &= bool(test_diagnostics[-1]["passed"])
    parent_edits_verified = bool(client_turns) and all(
        any(item.get("type") == "file_change" and item.get("status") == "completed"
                and any(Path(change.get("path", "")).name == "parent_task.py" and change.get("kind") == "update"
                        for change in item.get("changes", [])) for item in turn["items"])
        for turn in client_turns
    )
    parent_edits_verified = parent_edits_verified and len(parent_records) == len(child_records) == 1 and _parent_patch_after_reviews(parent_records[0], child_records[0])
    # Shell edits need not generate a CLI file_change item. Kernel-observed
    # writes plus an external digest change provide an alternative, only when
    # every child tool is independently proven inspection-only.
    child_inspection_proven = bool(child_records) and all(_child_inspect_only(record) for record in child_records)
    second_done = lifecycle_diagnostics.get("completion_timing", {}).get("second_done")
    review_end = datetime.fromisoformat(second_done.replace("Z", "+00:00")).timestamp() if second_done else None
    observed_edits = bool(client_turns) and child_inspection_proven and review_end is not None and all(
        turn["process_observation"].get("source_changed") is True
        and any(write.get("file") == "parent_task.py" and isinstance(write.get("timestamp"), (float, int))
                and write["timestamp"] >= review_end for write in turn["process_observation"].get("writes", []))
        for turn in client_turns
    )
    parent_edits_verified = parent_edits_verified or observed_edits
    if any(write.get("file") in {"test_parent_task.py", "test_resume_turn.py"}
           for turn in client_turns for write in turn["process_observation"].get("writes", [])):
        tests_and_terminals_verified = False
    # A real observed turn must also prove that the source and test files did
    # not change while its test process was running.  This closes the common
    # false-green path where a model edits the test or runs an old checkout.
    if any(item.get("process_verified") and item.get("source_sha_before") is not None
           for item in test_diagnostics):
        tests_and_terminals_verified = tests_and_terminals_verified and all(
            item.get("source_unchanged_during_test") is True
            and item.get("test_unchanged_during_test") is True
            for item in test_diagnostics
        )
    execution_passed = tests_and_terminals_verified and parent_edits_verified
    passed = lifecycle_passed and contexts_match and execution_passed and no_subagent_turn
    child_read_only = bool(child_records) and all(
        row["payload"].get("sandbox_policy", {}).get("type") == "read-only"
        for record in child_records for row in record["rows"] if row.get("type") == "turn_context"
    )
    child_inspection_verified = bool(child_records) and all(_child_inspect_only(record) for record in child_records)
    passed = passed and child_inspection_verified
    missing_evidence = []
    if require_process_evidence and any(
        not isinstance(turn.get("process_observation"), dict)
        or not turn["process_observation"].get("available")
        or "test_evidence" not in turn["process_observation"]
        for turn in client_turns
    ):
        missing_evidence.append("trusted_process_test_evidence")
        passed = False
    if tests_and_terminals_verified and not parent_edits_verified:
        # A shell edit need not emit file_change. Absence of attribution is
        # neither proof of model failure nor permission to accept the run.
        missing_evidence.append("parent_source_edit_attribution")
    attribution_only_gap = bool(missing_evidence) and lifecycle_passed and contexts_match and no_subagent_turn and child_inspection_verified
    return {
        "lifecycle_diagnostics": lifecycle_diagnostics,
        "execution_diagnostics": {
            "tests_and_terminals_verified": tests_and_terminals_verified,
            "test_cases": test_diagnostics,
            "process_observations": [
                json.loads(Path(str(path) + ".process.json").read_text())
                if Path(str(path) + ".process.json").is_file() else {"available": False}
                for path in client_outputs
            ],
            "test_commands": [
                dict(_test_command_diagnostic(
                    item,
                    "test_parent_task.py" if index == 0 else "test_resume_turn.py",
                    require_complete_suite=require_process_evidence,
                ),
                     thread_id=turn["thread_id"], turn_index=index)
                for index, turn in enumerate(client_turns)
                for item in turn["items"] if item.get("type") == "command_execution"
            ],
            "parent_edits_verified": parent_edits_verified,
            "cli_source_edit_counts": [sum(
                item.get("type") == "file_change" and item.get("status") == "completed"
                and any(Path(change.get("path", "")).name == "parent_task.py"
                        and change.get("kind") == "update" for change in item.get("changes", []))
                for item in turn["items"]) for turn in client_turns],
            "rollout_patch_call_count": sum(
                row.get("type") == "response_item"
                and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
                and row["payload"].get("name") == "apply_patch"
                for record in parent_records for row in record["rows"]),
        },
        "missing_evidence": missing_evidence,
        "child_read_only": child_read_only,
        "child_inspection_verified": child_inspection_verified,
        "child_sandbox_types": sorted({str(row["payload"].get("sandbox_policy", {}).get("type"))
                                       for record in child_records for row in record["rows"] if row.get("type") == "turn_context"}),
        "lifecycle_passed": lifecycle_passed,
        "contexts_match": contexts_match,
        "execution_passed": execution_passed,
        "no_subagent_turn_passed": no_subagent_turn,
        "parent_turn_ids": [turn["id"] for turn in parent_turns],
        "call_trace": [{key: call[key] for key in ("id", "name", "timestamp", "failed")} for call in calls][:128],
        "client_trace": [{"thread_id": turn["thread_id"], "terminal": turn["terminal"],
                          "terminal_error": turn.get("terminal_error"),
                          "items": [{key: item[key] for key in ("id", "type", "status", "exit_code") if key in item}
                                    for item in turn["items"]][:128]} for turn in client_turns],
        "child_models": child_models,
        "parent_models": parent_models,
        "child_completed_results": sorted(child_results),
        "parent_received_results": sorted(parent_results),
        "plaintext_child_handoffs": plaintext_handoffs,
        "encrypted_child_handoffs": encrypted_handoffs,
        "handoff_contract_passed": handoff_contract_passed,
        "portable_calls": sorted(portable_calls),
        "parent_spawn_call_count": call_names.count("spawn_agent"),
        "successful_child_creation_count": sum(call["creation_verified"] for call in calls),
        "parent_child_relationships": [
            {"child": child, "parent": parent} for child, parent in relationships
        ],
        "parent_test_tool_call_count": _parent_test_tool_call_count(parent_records),
        "parent_resume_test_tool_call_count": _parent_resume_test_tool_call_count(
            parent_records
        ),
        "passed": passed,
        "status": "passed" if passed and not missing_evidence else "unverified" if attribution_only_gap or not client_outputs or (require_process_evidence and missing_evidence) else "failed",
    }


def _scenario_prompt(*, collaboration_version: str, child_model: str, child_effort: str) -> str:
    if collaboration_version == "v2":
        collaboration_steps = (
            "Spawn exactly one child with agent_type `reviewer`, task_name `reviewer`, model "
            f"`{child_model}`, reasoning_effort `{child_effort}`, and fork_turns `none`. "
            "Wait for its actual review. Then use followup_task on that same child, wait for "
            "the actual follow-up result, and do not spawn another child."
        )
    else:
        collaboration_steps = (
            "Spawn exactly one child using the V1 spawn_agent tool with agent_type `reviewer`, "
            "fork_context `false`, and the review task below. Wait for its actual review. "
            "Close that child, resume_agent with the same id, then send_input to that id "
            "with the follow-up task. Wait for its actual result and close the child again. "
            "Do not spawn another child."
        )
    return (
        "This is an isolated real Collaboration E2E. Work only in the current directory. "
        "Read child_review.txt and parent_task.py. "
        f"{collaboration_steps} "
        "The child must inspect only: use only the exec_command tool with the exact command "
        "`cat parent_task.py child_review.txt` in the fixture directory; do not edit or run code. "
        "Include these inspection restrictions in both messages sent to the child. Its first response must contain "
        "E2E_CHILD_OK 323 and its follow-up response must contain E2E_FOLLOWUP_OK 667. "
        "After receiving both actual child results, the parent itself must edit parent_task.py so "
        "normalize(7) returns exactly `FIXED`. Use any available editing tool. "
        "Then run `\"$CODEXHUB_E2E_PYTHON\" -m unittest -q test_parent_task.py`. "
        "Report E2E_PARENT_IMPLEMENTED_OK 941 only after that command succeeds. Do not fabricate "
        "tool results or use a different model/provider."
    )


def _write_parent_fixture(work: Path) -> None:
    (work / "parent_task.py").write_text('def normalize(value):\n    return "BROKEN"\n', encoding="utf-8")
    (work / "test_parent_task.py").write_text(_fixture_test(False), encoding="utf-8")
    (work / "child_review.txt").write_text(
        "Review only. The required fix is that normalize(7) returns exactly FIXED.\n",
        encoding="utf-8",
    )


def _write_resume_fixture(work: Path) -> None:
    (work / "test_resume_turn.py").write_text(_fixture_test(True), encoding="utf-8")


def _fixture_test(resume: bool) -> str:
    return (
        "import os, sys\nfrom pathlib import Path\n"
        "if sys.version_info < (3, 13) or not os.environ.get('CODEXHUB_E2E_PYTHON') "
        "or Path(sys.executable).resolve() != Path(os.environ['CODEXHUB_E2E_PYTHON']).resolve():\n"
        "    raise RuntimeError('fixture_python_binding_mismatch')\n"
        "from e2e_test_recorder import EvidenceTestCase, register_suite\n"
        "from parent_task import normalize\n\n"
        + (
            "register_suite(['test_parent_task.ParentTaskTest.test_normalize', "
            "'test_resume_turn.ParentTaskTest.test_resume'])\n"
            if resume
            else "if not os.environ.get('CODEXHUB_E2E_COMBINED'):\n"
            "    register_suite(['test_parent_task.ParentTaskTest.test_normalize'])\n"
        )
        + "\nclass ParentTaskTest(EvidenceTestCase):\n"
        + (
            "    def test_resume(self):\n        self.assertEqual(normalize(8), 'RESUMED')\n\n"
            if resume
            else "    def test_normalize(self):\n        self.assertEqual(normalize(7), 'FIXED')\n\n"
        )
    )


def _expected_cases_for_test(test_name: str) -> tuple[str, ...]:
    module = test_name[:-3]
    if module == "test_resume_turn":
        return (
            "test_parent_task.ParentTaskTest.test_normalize",
            "test_resume_turn.ParentTaskTest.test_resume",
        )
    return ("test_parent_task.ParentTaskTest.test_normalize",)


def _verify_parent_fixture(work: Path) -> dict[str, object]:
    return _verify_fixture(
        work,
        source_name="parent_task.py",
        test_name="test_parent_task.py",
        expected_source='return "FIXED"',
        fixed_key="parent_fixture_fixed",
        exit_code_key="host_test_exit_code",
    )


def _verify_resume_fixture(work: Path) -> dict[str, object]:
    return _verify_fixture(
        work,
        source_name="parent_task.py",
        test_name="test_resume_turn.py",
        expected_source="RESUMED",
        fixed_key="resume_fixture_fixed",
        exit_code_key="resume_host_test_exit_code",
    )


def _verify_fixture(
    work: Path,
    *,
    source_name: str,
    test_name: str,
    expected_source: str,
    fixed_key: str,
    exit_code_key: str,
) -> dict[str, object]:
    source = work / source_name
    resume = test_name == "test_resume_turn.py"
    test_names = ("test_parent_task.py", test_name) if resume else (test_name,)
    expected_tests = {
        name: _fixture_test(name == "test_resume_turn.py")
        for name in test_names
    }
    expected_cases = (
        (
            "test_parent_task.ParentTaskTest.test_normalize",
            "test_resume_turn.ParentTaskTest.test_resume",
        )
        if resume
        else ("test_parent_task.ParentTaskTest.test_normalize",)
    )
    source_sha_before = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
    test_sha_before = _fixture_digest(work, test_names)
    if (
        not source.is_file()
        or any(not (work / name).is_file() or (work / name).read_text(encoding="utf-8") != expected for name, expected in expected_tests.items())
    ):
        return {fixed_key: False, exit_code_key: 1,
                "expected_cases": list(expected_cases), "verified_cases": [],
                "source_sha_before": source_sha_before, "source_sha_after": source_sha_before,
                "test_sha_before": test_sha_before, "test_sha_after": test_sha_before,
                "test_evidence_verified": False}
    # This deliberately tiny fixture needs only a pure value function. Never
    # import arbitrary model-produced code into the trusted unittest process:
    # os._exit(0), monkeypatching unittest, or file writes could forge success.
    # Unsupported implementations remain unaccepted, not silently executed.
    source_text = source.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source_text)
        allowed = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg,
                   ast.Return, ast.If, ast.IfExp, ast.Compare, ast.Eq, ast.NotEq,
                   ast.Constant, ast.Name, ast.Load, ast.Expr, ast.Pass)
        safe = (
            len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
            and tree.body[0].name == "normalize"
            and not tree.body[0].decorator_list
            and not tree.body[0].args.defaults
            and not any(tree.body[0].args.kw_defaults)
            and all(isinstance(node, allowed) for node in ast.walk(tree))
        )
    except (SyntaxError, ValueError, RecursionError):
        safe = False
    if not safe:
        return {fixed_key: False, exit_code_key: None,
                "validator_rejection": "unsupported_fixture_source",
                "expected_cases": list(expected_cases), "verified_cases": [],
                "source_sha_before": source_sha_before, "source_sha_after": source_sha_before,
                "test_sha_before": test_sha_before, "test_sha_after": test_sha_before,
                "test_evidence_verified": False}
    trusted_evidence: dict[str, object] = {}
    try:
        # Execute a copied source against the harness-owned test in a separate
        # directory. Neither mutable fixture tests nor source spelling decide PASS.
        with tempfile.TemporaryDirectory(prefix="codexhub-trusted-validator-") as directory:
            trusted = Path(directory)
            (trusted / source_name).write_text(source_text, encoding="utf-8")
            for name, expected in expected_tests.items():
                (trusted / name).write_text(expected, encoding="utf-8")
            recorder = ROOT / "scripts" / "e2e_test_recorder.py"
            shutil.copy2(recorder, trusted / recorder.name)
            shutil.copy2(ROOT / "src-python" / "python_runtime_contract.py", trusted / "python_runtime_contract.py")
            evidence_path = trusted / "test-evidence.jsonl"
            verifier_env = dict(os.environ)
            verifier_env["CODEXHUB_E2E_PYTHON"] = str(Path(sys.executable).resolve())
            verifier_env["CODEXHUB_E2E_TEST_EVIDENCE"] = str(evidence_path)
            verifier_env["CODEXHUB_E2E_TEST_FILE"] = test_name
            verifier_env["CODEXHUB_E2E_TEST_FILES"] = os.pathsep.join(test_names)
            if resume:
                verifier_env["CODEXHUB_E2E_COMBINED"] = "1"
            verifier_env["CODEXHUB_E2E_SOURCE_FILE"] = source_name
            verifier_env["PYTHONPATH"] = os.pathsep.join(
                [str(trusted), str(ROOT / "src-python")]
            )
            result = subprocess.run(
                [sys.executable, "-E", "-s", "-m", "unittest", "-q", *test_names],
                cwd=trusted, env=verifier_env, capture_output=True, text=True, timeout=20,
            )
            from collaboration_process_evidence import read_test_evidence
            trusted_evidence = read_test_evidence(
                evidence_path,
                expected_cases=expected_cases,
                recorder_path=trusted / recorder.name,
                recorder_sha256=hashlib.sha256(recorder.read_bytes()).hexdigest(),
            )
        test_exit_code: int | None = result.returncode
    except (OSError, subprocess.TimeoutExpired):
        test_exit_code = None
    source_sha_after = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
    test_sha_after = _fixture_digest(work, test_names)
    evidence_covers_final_files = (
        trusted_evidence.get("source_sha_at_test_finish") == source_sha_after
        and trusted_evidence.get("test_sha_at_test_finish") == test_sha_after
    )
    return {
        fixed_key: test_exit_code == 0
        and bool(trusted_evidence.get("complete"))
        and evidence_covers_final_files,
        exit_code_key: test_exit_code,
        "expected_cases": list(expected_cases),
        "verified_cases": list(trusted_evidence.get("verified_cases", [])),
        "test_evidence_verified": bool(trusted_evidence.get("complete"))
        and evidence_covers_final_files,
        "test_evidence_covers_final_files": evidence_covers_final_files,
        "trusted_test_evidence": trusted_evidence,
        "source_sha_before": source_sha_before,
        "source_sha_after": source_sha_after,
        "test_sha_before": test_sha_before,
        "test_sha_after": test_sha_after,
    }


def _no_subagent_turn_prompt() -> str:
    return (
        "This is a new user turn in the same parent task. Do not inspect, call, "
        "resume, create, or delegate to any subagent or collaboration tool. Work "
        "yourself: edit parent_task.py so normalize(7) remains exactly `FIXED` and "
        "normalize(8) returns exactly `RESUMED`. Use any available editing tool. "
        "Then run `\"$CODEXHUB_E2E_PYTHON\" -m unittest -q "
        "test_parent_task.py test_resume_turn.py`. Report "
        "E2E_NO_SUBAGENT_TURN_OK 818 only after that command succeeds."
    )


def _stop_client_process(client: subprocess.Popen[object]) -> None:
    """Bound a timed-out live probe without leaving its Codex children alive."""
    if client.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(client.pid, signal.SIGTERM)
    else:
        client.terminate()
    try:
        client.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(client.pid, signal.SIGKILL)
        else:
            client.kill()
        client.wait(timeout=10)


def _proc_tree_snapshot(root_pid: int, *, client_entrypoint: str | None = None) -> tuple[dict[int, set[int]], set[int]]:
    """Capture the live descendant tree of one traced client process.

    ``strace`` records syscall-level fork edges, but runtimes using
    ``posix_spawn``/``clone3`` can leave no portable edge in the text trace.
    A short-lived /proc snapshot closes that attribution gap without retaining
    command arguments, request bodies, or environment values.
    """
    parent_of: dict[int, int] = {}
    executable: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            # The comm field may contain spaces/parentheses; the final `)` is
            # the delimiter before state and ppid.
            marker = stat.rfind(")")
            fields = stat[marker + 2 :].split()
            pid = int(entry.name)
            ppid = int(fields[1])
            parent_of[pid] = ppid
            try:
                executable[pid] = str((entry / "exe").resolve())
            except OSError:
                executable[pid] = ""
        except (OSError, ValueError, IndexError):
            continue
    descendants: set[int] = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent_of.items():
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    edges: dict[int, set[int]] = {}
    for pid in descendants:
        children = {child for child, ppid in parent_of.items() if ppid == pid and child in descendants}
        if children:
            edges[pid] = children
    client_pids: set[int] = set()
    if client_entrypoint:
        for pid in descendants:
            if Path(executable.get(pid, "")).name == client_entrypoint:
                client_pids.add(pid)
    return edges, client_pids


def _fixture_digest(work: Path, names: tuple[str, ...]) -> str | None:
    digest = hashlib.sha256()
    for name in names:
        path = work / name
        try:
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _run_client(
    command: list[str],
    *,
    environment: dict[str, str],
    output: Path,
    working_directory: Path,
    timeout: int,
    observe_processes: bool = False,
    test_evidence: Path | None = None,
    expected_cases: tuple[str, ...] = (),
    test_name: str | None = None,
) -> int | None:
    """Run one explicitly requested client turn in the isolated fixture root."""
    if test_evidence is not None:
        environment["CODEXHUB_E2E_TEST_EVIDENCE"] = str(test_evidence)
    test_names = (
        ("test_parent_task.py", "test_resume_turn.py")
        if test_name == "test_resume_turn.py"
        else (test_name,) if test_name else ()
    )
    if test_name:
        environment["CODEXHUB_E2E_TEST_FILE"] = test_name
        environment["CODEXHUB_E2E_SOURCE_FILE"] = "parent_task.py"
        environment["CODEXHUB_E2E_TEST_FILES"] = os.pathsep.join(test_names)
    source_path = working_directory / "parent_task.py"
    source_before = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None
    test_before = _fixture_digest(working_directory, test_names) if test_names else None
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    client_entrypoint = Path(command[0]).name if command else None
    observed_tree: dict[int, set[int]] = {}
    observed_client_pids: set[int] = set()
    if observe_processes:
        tracer = shutil.which("strace")
        if sys.platform != "linux" or not tracer:
            raise RuntimeError("process_observer_unavailable")
        command = [tracer, "-ff", "-qq", "-yy", "-ttt", "-s", "8192", "-e",
                   "trace=execve,exit_group,openat,clone,clone3,fork,vfork", "-o",
                   str(output) + ".process"] + command
    with output.open("a", encoding="utf-8") as stream:
        client = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            env=environment,
            cwd=working_directory,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            if not observe_processes:
                return client.wait(timeout=timeout)
            deadline = time.monotonic() + timeout
            exit_code = None
            while exit_code is None:
                edges, client_pids = _proc_tree_snapshot(client.pid, client_entrypoint=client_entrypoint)
                for parent, children in edges.items():
                    observed_tree.setdefault(parent, set()).update(children)
                observed_client_pids.update(client_pids)
                exit_code = client.poll()
                if exit_code is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(min(0.05, remaining))
            return exit_code
        except subprocess.TimeoutExpired:
            _stop_client_process(client)
            return None
        finally:
            if observe_processes:
                from collaboration_process_evidence import read_process_evidence
                from collaboration_process_evidence import read_test_evidence
                interpreter_name = environment.get("CODEXHUB_E2E_PYTHON") or str(sys.executable)
                evidence = read_process_evidence(
                    Path(str(output) + ".process"),
                    working_directory,
                    Path(interpreter_name),
                    expected_cases=expected_cases,
                    recorder_path=RECORDER_PATH,
                    evidence_path=test_evidence,
                    client_entrypoint=client_entrypoint,
                    observed_process_tree=observed_tree,
                    observed_client_pids=observed_client_pids,
                )
                source_after = hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None
                test_after = _fixture_digest(working_directory, test_names) if test_names else None
                evidence["source_sha_before"] = source_before
                evidence["source_sha_after"] = source_after
                evidence["test_sha_before"] = test_before
                evidence["test_sha_after"] = test_after
                evidence["source_changed"] = source_before is not None and source_after != source_before
                evidence["test_changed"] = test_before is not None and test_after != test_before
                evidence["started_at"] = started_at
                evidence["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                evidence["test_name"] = test_name
                evidence["test_evidence"] = read_test_evidence(
                    test_evidence,
                    expected_cases=expected_cases,
                    recorder_path=RECORDER_PATH,
                    recorder_sha256=RECORDER_SHA256,
                ) if test_evidence is not None else {
                    "available": False, "complete": False, "rejection": "test_evidence_not_configured"
                }
                Path(str(output) + ".process.json").write_text(json.dumps(evidence), encoding="utf-8")


def collect_failure_signals(path: Path) -> list[dict]:
    """Retain only structural error codes and known diagnostic categories."""
    signals = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
            if event.get("type") != "error":
                continue
            message = event.get("message", "")
            payload = json.loads(message)
        except (ValueError, TypeError):
            continue
        error = payload.get("codexhub_error", {})
        details = error.get("details", {})
        signal = {key: error[key] for key in ("code", "source") if key in error}
        signal.update({key: details[key] for key in (
            "status", "reason", "classification", "failure_class", "type",
        ) if key in details})
        signal["categories"] = [name for name in (
            "not supported", "does not exist", "model_not_found", "missing_catalog_model",
            "encrypted_agent_message_unavailable", "configured schema", "tool_choice",
        ) if name in message.lower()]
        if signal not in signals:
            signals.append(signal)
    return signals


def collect_gateway_log_signals(path: Path) -> list[dict[str, object]]:
    """Extract bounded, redacted gateway failure classifications for E2E reports."""
    if not path.is_file():
        return []
    signals: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        lowered = line.lower()
        if not any(token in lowered for token in ("error", "exception", "traceback", "failed", "tool_compatibility", "stream")):
            continue
        # Keep only a bounded diagnostic fragment; never retain request bodies,
        # auth material, or full stack traces in the report.
        clean = re.sub(r"(?:sk-|Bearer )[^ \"']+", "<REDACTED>", line)
        clean = re.sub(r"/(?:home|tmp)/[^\s\"']+", "<PATH>", clean)
        clean = re.sub(r"[A-Za-z0-9+/]{48,}", "<TOKEN>", clean)
        signal: dict[str, object] = {"line": clean[:1200]}
        for key in ("classification", "code", "status", "reason", "failure_class"):
            match = re.search(rf"[\"']?{key}[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.-]+)", line)
            if match:
                signal[key] = match.group(1)
        if signal not in signals:
            signals.append(signal)
        if len(signals) >= 32:
            break
    return signals


def classify_failure_signals(signals: list[dict]) -> str | None:
    # The outer source names the route, not necessarily the component that
    # rejected it. Local boundary codes take precedence over that label.
    from collaboration_adapter import (
        COLLABORATION_BOUNDARY_ERROR_CODE, WORKER_SELECTOR_ERROR_CODE,
        WORKER_BINDING_ERROR_CODE,
    )
    boundary_codes = {COLLABORATION_BOUNDARY_ERROR_CODE, WORKER_SELECTOR_ERROR_CODE,
                      WORKER_BINDING_ERROR_CODE}
    if any(signal.get("type") in boundary_codes or signal.get("code") in boundary_codes
           for signal in signals):
        return "gateway_collaboration_boundary"
    if any(signal.get("failure_class") == "permanent" for signal in signals):
        return "provider_request_permanent"
    return None


def classify_request_trace(observations: list[dict]) -> str | None:
    """Classify a bounded provider response without treating it as Gateway logic."""
    statuses = [
        item.get("upstream_status") for item in observations
        if isinstance(item, dict) and isinstance(item.get("upstream_status"), int)
    ]
    if any(status >= 500 for status in statuses):
        return "provider_request_transient"
    if any(400 <= status < 500 for status in statuses):
        return "provider_request_permanent"
    return None


def write_reviewer_config(client_home: Path, model: str, effort: str) -> Path:
    """Use the standalone custom-agent contract, not retired role tables."""
    path = client_home / "agents" / "reviewer.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'name = "reviewer"\ndescription = "Read-only fixture reviewer"\n'
        'developer_instructions = "Inspect only the requested fixture files; do not edit or execute code."\n'
        'sandbox_mode = "read-only"\n'
        + f'model = {json.dumps(model)}\nmodel_reasoning_effort = {json.dumps(effort)}\n',
        encoding="utf-8",
    )
    return path


def _git_revision_state(gateway_root: Path) -> dict[str, object]:
    """Capture source identity without treating evidence files as source edits.

    The checkout intentionally keeps untracked, local evidence outside the
    candidate commit.  ``gateway_dirty`` therefore describes tracked changes
    only; the bounded untracked count remains visible so a report cannot hide
    a genuinely modified source tree.
    """

    revision = subprocess.check_output(
        ["git", "-C", str(gateway_root), "rev-parse", "HEAD"], text=True,
    ).strip()
    tracked = subprocess.check_output(
        ["git", "-C", str(gateway_root), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    ).strip()
    untracked = subprocess.check_output(
        ["git", "-C", str(gateway_root), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    ).splitlines()
    untracked_count = sum(1 for line in untracked if line.startswith("?? "))
    return {
        "gateway_sha": revision,
        "gateway_dirty": bool(tracked),
        "gateway_tracked_status": tracked,
        "gateway_untracked_count": untracked_count,
    }


def _safe_matrix_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "case"


def _run_trusted_matrix(args: argparse.Namespace) -> int:
    """Run the fixed A/B matrix through this same single-scenario harness.

    This is deliberately an orchestration mode, not a second evidence
    implementation.  Every child process writes the normal v8 report, while
    this function only joins bounded structural fields and classifications.
    """

    baseline_root = args.baseline_root.resolve() if args.baseline_root else None
    candidate_root = args.gateway_root.resolve()
    if baseline_root is None or not baseline_root.is_dir():
        report = {
            "report_version": 1,
            "evidence_contract": "codexhub.e2e-matrix.v1",
            "status": "unverified",
            "passed": False,
            "failure_classification": "baseline_root_required",
            "candidate_root": str(candidate_root),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report))
        return 2

    cases = build_trusted_matrix_cases(candidate_repeats=args.candidate_repeats)
    baseline_cases = build_trusted_baseline_cases()
    output_dir = args.matrix_output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    child_script = Path(__file__).resolve()
    source_home = args.source_home.expanduser().resolve()
    rows: list[dict[str, object]] = []

    def execute(label: str, root: Path, case: MatrixCase, run_index: int) -> None:
        output = output_dir / (
            f"{label}-{_safe_matrix_name(case.parent_model)}-"
            f"{case.collaboration_version}-{run_index}.json"
        )
        command = [
            sys.executable,
            str(child_script),
            "--source-home", str(source_home),
            "--parent-model", case.parent_model,
            "--parent-effort", case.effort,
            "--parent-collaboration-version", case.collaboration_version,
            "--gateway-root", str(root),
            "--timeout", str(args.timeout),
            "--output", str(output),
        ]
        started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                env=dict(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=args.timeout + 60,
                check=False,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            exit_code = None
        row: dict[str, object] = {
            "label": label,
            "run_index": run_index,
            "parent_model": case.parent_model,
            "effort": case.effort,
            "collaboration_version": case.collaboration_version,
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "exit_code": exit_code,
            "report_path": str(output),
        }
        if output.is_file():
            try:
                child = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                child = {}
            if isinstance(child, dict):
                for key in (
                    "status", "passed", "failure_classification", "gateway_sha",
                    "gateway_dirty", "request_namespace_verified", "upstream_statuses",
                    "client_exit_code", "resume_client_exit_code",
                ):
                    if key in child:
                        row[key] = child[key]
        if "status" not in row:
            row["status"] = "unverified"
            row["failure_classification"] = "matrix_child_no_report"
        rows.append(row)

    # Baseline A is intentionally four one-shot reference runs.
    for case in baseline_cases:
        execute("baseline", baseline_root, case, 1)
    # Candidate B is the fixed 13-scenario matrix (Grok/Muse x3; CommandCode x1).
    for case in cases:
        for run_index in range(1, case.repeats + 1):
            execute("candidate", candidate_root, case, run_index)

    counts = {"passed": 0, "failed": 0, "unverified": 0}
    for row in rows:
        status = row.get("status")
        if status not in counts:
            status = "unverified"
            row["status"] = status
        counts[status] += 1
    candidate_rows = [row for row in rows if row.get("label") == "candidate"]
    candidate_complete = len(candidate_rows) == sum(case.repeats for case in cases)
    status = "passed" if candidate_complete and all(
        row.get("status") == "passed" and row.get("passed") is True
        for row in candidate_rows
    ) and all(row.get("gateway_dirty") is False for row in candidate_rows) else "failed"
    if any(row.get("status") == "unverified" for row in rows):
        status = "unverified"
    matrix = {
        "report_version": 1,
        "evidence_contract": "codexhub.e2e-matrix.v1",
        "cli_version": "codex-cli 0.153.4",
        "status": status,
        "passed": status == "passed",
        "candidate_root": str(candidate_root),
        "baseline_root": str(baseline_root),
        "candidate_revision": _git_revision_state(candidate_root),
        "baseline_revision": _git_revision_state(baseline_root),
        "candidate_repeat_policy": args.candidate_repeats,
        "expected_candidate_scenarios": sum(case.repeats for case in cases),
        "expected_baseline_scenarios": len(baseline_cases),
        "counts": counts,
        "runs": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(matrix))
    return 0 if status == "passed" else (2 if status == "unverified" else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--parent-model", default=DEFAULT_PARENT)
    parser.add_argument("--parent-effort")
    parser.add_argument("--parent-collaboration-version", choices=("v1", "v2"), default="v2")
    parser.add_argument("--refresh-official", action="store_true")
    parser.add_argument(
        "--observe-processes", action=argparse.BooleanOptionalAction, default=True,
        help="capture the real test process and recorder evidence (enabled by default)",
    )
    parser.add_argument("--child-model")
    parser.add_argument("--child-effort")
    parser.add_argument("--gateway-root", type=Path, default=ROOT)
    parser.add_argument(
        "--matrix", action="store_true",
        help="orchestrate the fixed baseline/candidate matrix through this harness",
    )
    parser.add_argument(
        "--baseline-root", type=Path,
        help="clean checkout of the legacy baseline (required with --matrix)",
    )
    parser.add_argument(
        "--candidate-repeats", type=int, default=3,
        help="candidate repeats for Grok/Muse matrix groups (default: 3)",
    )
    parser.add_argument(
        "--matrix-output-dir", type=Path, default=Path("test-results/third-party-matrix"),
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path, default=Path("test-results/third-party-collaboration.json"))
    args = parser.parse_args()
    gateway_root = args.gateway_root.resolve()
    cli_version = subprocess.check_output(["codex", "--version"], text=True).strip()
    if cli_version != "codex-cli 0.153.4":
        parser.error("requires codex-cli 0.153.4")
    if args.matrix:
        if args.candidate_repeats < 1:
            parser.error("--candidate-repeats must be positive")
        return _run_trusted_matrix(args)
    source = args.source_home.expanduser().resolve()
    parent_effort = args.parent_effort or DEFAULT_REASONING.get(args.parent_model, "high")
    child_model = args.child_model or args.parent_model
    child_effort = args.child_effort or DEFAULT_REASONING.get(child_model, parent_effort)
    required = (
        "auth.json", "proxy/settings.json", "proxy/official-editor-catalog.json", "proxy/config/providers.toml",
        "model-catalogs/codexhub-model-catalog.json",
    )
    if not all((source / name).is_file() for name in required):
        parser.error("source home lacks required isolated Gateway session or catalog inputs")
    if any(model.startswith("xai/") for model in (args.parent_model, child_model)) and not (
        source / "proxy/xai_auth.json"
    ).is_file():
        parser.error("the selected xAI model requires source-home/proxy/xai_auth.json")
    report = {
        "report_version": 8,
        "cli_version": cli_version,
        **_git_revision_state(gateway_root),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parent_model": args.parent_model,
        "parent_effort": parent_effort,
        "child_model": child_model,
        "child_effort": child_effort,
        "parent_collaboration_version": args.parent_collaboration_version,
        "process_observer_required": True,
        "process_observer_requested": args.observe_processes,
        "evidence_contract": "codexhub.e2e-evidence.v8",
        "passed": False,
    }
    selected_models = (args.parent_model, child_model)
    commandcode_confirmations: dict[str, dict[str, object]] = {}
    for selected_model in sorted(set(selected_models)):
        if selected_model.startswith("commandcode/") and "deepseek" in selected_model.lower():
            selected_effort = parent_effort if selected_model == args.parent_model else child_effort
            confirmation = confirm_commandcode_deepseek_41(
                source / "proxy/config/providers.toml",
                selected_model=selected_model,
                requested_effort=selected_effort,
            )
            commandcode_confirmations[selected_model] = confirmation
            if not confirmation.get("confirmed"):
                report["commandcode_model_confirmation"] = confirmation
                report["status"] = "unverified"
                report["failure_classification"] = "commandcode_deepseek_v4_1_flash_unconfirmed"
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report))
                return 2
    if commandcode_confirmations:
        report["commandcode_model_confirmations"] = commandcode_confirmations
    with tempfile.TemporaryDirectory(prefix="codexhub-collaboration-e2e-") as directory, ExitStack() as cleanup:
        private = Path(directory)
        work = private / "fixture"
        work.mkdir()
        server_home, client_home = private / "server", private / "client"
        for home in (server_home, client_home):
            home.mkdir()
            shutil.copy2(source / "auth.json", home / "auth.json")
        for name in required[1:]:
            target = server_home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        for selected_model, confirmation in commandcode_confirmations.items():
            report.setdefault("isolated_provider_config", {})[selected_model] = (
                install_confirmed_commandcode_model(
                    source / "proxy/config/providers.toml",
                    server_home / "proxy/config/providers.toml",
                    confirmation,
                    collaboration_version=args.parent_collaboration_version,
                )
            )
        if any(model.startswith("xai/") for model in (args.parent_model, child_model)):
            xai_target = server_home / "proxy/xai_auth.json"
            xai_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / "proxy/xai_auth.json", xai_target)
        catalog = client_home / "catalog.json"
        shutil.copy2(source / "model-catalogs/codexhub-model-catalog.json", catalog)
        # Read the candidate's bundled defaults through the production provider
        # loader. A copied live catalog may still select a retired V1 runtime.
        from providers_config import build_external_model_index, load_providers
        candidates = build_external_model_index(
            load_providers(server_home / "proxy/config/providers.toml"), require_api_key=False,
        )
        catalog_payload = json.loads(catalog.read_text())
        if args.refresh_official:
            refreshed = subprocess.run(
                [sys.executable, str(ROOT / "src-python/official_catalog.py"),
                 "--client-version", "0.153.4", "--timeout", "20"],
                env=dict(os.environ, CODEX_HOME=str(server_home)),
                capture_output=True, text=True, timeout=30, check=True,
            )
            fresh_models = json.loads(refreshed.stdout)["models"]
            by_slug = {model["slug"]: model for model in catalog_payload["models"]}
            for model in fresh_models:
                by_slug[model["slug"]] = model
            catalog_payload["models"] = list(by_slug.values())
            (server_home / "model-catalogs/codexhub-model-catalog.json").write_text(json.dumps(catalog_payload))
            report["refreshed_official_models"] = len(fresh_models)
        for model in catalog_payload["models"]:
            candidate = candidates.get(model.get("slug"), {})
            version = candidate.get("multi_agent_version")
            if version in {"v1", "v2"}:
                model["multi_agent_version"] = version
            if model.get("slug") in {args.parent_model, child_model}:
                model["multi_agent_version"] = args.parent_collaboration_version
        for selected_model, confirmation in commandcode_confirmations.items():
            inject_confirmed_model_into_catalog(
                catalog_payload,
                confirmation,
                collaboration_version=args.parent_collaboration_version,
            )
        catalog.write_text(json.dumps(catalog_payload))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        key = secrets.token_hex(32)
        server_env = dict(os.environ, CODEX_HOME=str(server_home), CODEX_PROXY_GATEWAY_CLIENT_KEY=key)
        client_env = dict(os.environ, CODEX_HOME=str(client_home), CODEXHUB_E2E_GATEWAY_KEY=key)
        for environment in (server_env, client_env):
            for name in ("CODEXHUB_CODEX_TARGET_HOME", "CODEXHUB_RUNTIME_HOME", "CODEXHUB_HOME", "CODEX_PROXY_HOME"):
                environment.pop(name, None)
        client_env["CODEXHUB_E2E_PYTHON"] = sys.executable
        client_env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "scripts"), str(ROOT / "src-python"), os.environ.get("PYTHONPATH", "")]
        )
        reviewer_config = write_reviewer_config(client_home, child_model, child_effort)
        report["reviewer_configuration_sha256"] = hashlib.sha256(reviewer_config.read_bytes()).hexdigest()
        observer = RequestObserver(port)
        cleanup.callback(observer.close)
        (client_home / "config.toml").write_text(
            f'model_provider = "custom"\nmodel = {json.dumps(args.parent_model)}\n'
            f'model_reasoning_effort = {json.dumps(parent_effort)}\n'
            'approval_policy = "never"\nsandbox_mode = "workspace-write"\n'
            f'model_catalog_json = {json.dumps(str(catalog))}\n'
            '[model_providers.custom]\nname = "candidate gateway"\n'
            f'base_url = "http://127.0.0.1:{observer.port}/v1"\nwire_api = "responses"\n'
            'env_key = "CODEXHUB_E2E_GATEWAY_KEY"\nsupports_websockets = false\n'
            '[features]\nmulti_agent = true\napps = false\nplugins = false\n'
            'responses_websockets = false\nresponses_websockets_v2 = false\n'
        )
        _write_parent_fixture(work)
        report["configuration_sha256"] = hashlib.sha256((client_home / "config.toml").read_bytes()).hexdigest()
        report["catalog_sha256"] = hashlib.sha256(catalog.read_bytes()).hexdigest()
        report["fixture_test_sha256"] = {name: hashlib.sha256(_fixture_test(resume).encode()).hexdigest()
                                        for name, resume in (("first", False), ("second", True))}
        with (private / "gateway.log").open("w") as log:
            server = subprocess.Popen(
                [sys.executable, str(gateway_root / "src-python/codex_proxy.py"), "--port", str(port)],
                env=dict(server_env, PYTHONPATH=str(gateway_root / "src-python")), stdout=log, stderr=log, cwd=gateway_root,
            )
            try:
                for _ in range(100):
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("candidate_gateway_unavailable")
                client_output = private / "client-first.jsonl"
                client_outputs = (client_output,)
                first_test_evidence = private / "test-first.evidence.jsonl"
                client_env["CODEXHUB_E2E_TEST_EVIDENCE"] = str(first_test_evidence)
                client_exit_code = _run_client(
                    [
                        "codex",
                        "exec",
                        "--json",
                        "--skip-git-repo-check",
                        "-C",
                        str(work),
                        _scenario_prompt(
                            collaboration_version=args.parent_collaboration_version,
                            child_model=child_model,
                            child_effort=child_effort,
                        ),
                    ],
                    environment=client_env,
                    output=client_output,
                    working_directory=work,
                    timeout=args.timeout,
                    observe_processes=args.observe_processes,
                    test_evidence=first_test_evidence,
                    expected_cases=_expected_cases_for_test("test_parent_task.py"),
                    test_name="test_parent_task.py",
                )
                report["client_exit_code"] = client_exit_code
                report["failure_signals"] = collect_failure_signals(client_output)
                first_evidence = collect_evidence(
                    client_home,
                    child_model,
                    args.parent_collaboration_version,
                    parent_model=args.parent_model,
                    parent_effort=parent_effort,
                    child_effort=child_effort,
                    client_outputs=client_outputs,
                    fixture_directory=work,
                    require_process_evidence=True,
                )
                report["first_turn_evidence"] = first_evidence
                first_fixture = _verify_parent_fixture(work)
                report["client_exit_code"] = client_exit_code
                report.update(first_fixture)
                report["resume_client_exit_code"] = None
                report["resume_attempted"] = False
                if client_exit_code is None:
                    report["failure_type"] = "TimeoutExpired"

                # This is an intentional second user turn, not a retry: run it
                # only after the first turn has independently demonstrated the
                # complete parent/child lifecycle and parent-owned fix.
                # Resume exactly the parent thread that produced the first
                # client stream.  Never select by model label or recency.
                first_client_thread_id = _read_client_turn(client_outputs[0])["thread_id"]
                report["parent_thread_id"] = first_client_thread_id
                parent_session_id = _parent_session_id(client_home, first_client_thread_id)
                report["parent_session_id"] = parent_session_id
                first_turn_passed = bool(
                    first_evidence["passed"]
                    and client_exit_code == 0
                    and first_fixture["parent_fixture_fixed"]
                    and first_fixture["host_test_exit_code"] == 0
                    and first_evidence["parent_test_tool_call_count"] > 0
                )
                if first_turn_passed and parent_session_id is not None:
                    _write_resume_fixture(work)
                    report["resume_attempted"] = True
                    client_output = private / "client-second.jsonl"
                    client_outputs += (client_output,)
                    second_test_evidence = private / "test-second.evidence.jsonl"
                    client_env["CODEXHUB_E2E_TEST_EVIDENCE"] = str(second_test_evidence)
                    client_env["CODEXHUB_E2E_COMBINED"] = "1"
                    report["resume_client_exit_code"] = _run_client(
                        [
                            "codex",
                            "exec",
                            "resume",
                            "--json",
                            "--skip-git-repo-check",
                            parent_session_id,
                            # Re-run the first-round assertion together with
                            # the new requirement; a follow-up must not be
                            # accepted on a different or reduced test set.
                            _no_subagent_turn_prompt(),
                        ],
                        environment=client_env,
                        output=client_output,
                        working_directory=work,
                        timeout=args.timeout,
                        observe_processes=args.observe_processes,
                        test_evidence=second_test_evidence,
                        expected_cases=_expected_cases_for_test("test_resume_turn.py"),
                        test_name="test_resume_turn.py",
                    )
                elif first_turn_passed:
                    report["failure_type"] = "parent_session_unavailable"

                final_evidence = collect_evidence(
                    client_home,
                    child_model,
                    args.parent_collaboration_version,
                    parent_model=args.parent_model,
                    parent_effort=parent_effort,
                    child_effort=child_effort,
                    client_outputs=client_outputs,
                    fixture_directory=work,
                    require_process_evidence=True,
                )
                report.update(final_evidence)
                report["final_evidence"] = final_evidence
                report.update(_verify_resume_fixture(work))
                report["passed"] = bool(
                    report["passed"]
                    and client_exit_code == 0
                    and report["parent_fixture_fixed"]
                    and report["host_test_exit_code"] == 0
                    and report["parent_test_tool_call_count"] > 0
                    and report["resume_attempted"]
                    and report["resume_client_exit_code"] == 0
                    and report["resume_fixture_fixed"]
                    and report["resume_host_test_exit_code"] == 0
                    and report["parent_resume_test_tool_call_count"] > 0
                )
                if not report["passed"]:
                    report["failure_signals"] = collect_failure_signals(client_output)
                    signal_classification = classify_failure_signals(report["failure_signals"])
                    if signal_classification:
                        report["failure_classification"] = signal_classification
                    elif client_exit_code is None or (
                        report["resume_attempted"]
                        and report["resume_client_exit_code"] is None
                    ):
                        report["failure_classification"] = "client_turn_timeout"
                    elif (
                        report["resume_attempted"]
                        and report["resume_client_exit_code"] == 0
                        and NO_SUBAGENT_TURN_SENTINEL
                        in report["parent_received_results"]
                        and report["parent_resume_test_tool_call_count"] > 0
                        and not report["resume_fixture_fixed"]
                    ):
                        # The client emitted an affirmative final after using
                        # local tools, but the independently run fixture still
                        # fails. Do not retry or hide this with a forced final.
                        report["failure_classification"] = (
                            "parent_resume_fixture_not_completed"
                        )
                    else:
                        report["failure_classification"] = "collaboration_e2e_incomplete"
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                report["failure_type"] = type(error).__name__
                report["failure_classification"] = str(error) if isinstance(error, RuntimeError) else "infrastructure_error"
                report["status"] = "unverified"
                report["passed"] = False
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                report["request_trace"] = observer.observations
                report["gateway_log_signals"] = collect_gateway_log_signals(private / "gateway.log")
                report["upstream_statuses"] = sorted({
                    entry.get("upstream_status")
                    for entry in observer.observations
                    if isinstance(entry.get("upstream_status"), int)
                })
                expected_protocol = "collaboration_v1" if args.parent_collaboration_version == "v1" else "collaboration_v2"
                protocols = {entry.get("protocol") for entry in observer.observations if entry.get("protocol")}
                report["request_namespace_verified"] = protocols == {expected_protocol}
                if not report["request_namespace_verified"]:
                    report["passed"] = False
                    report["status"] = "unverified"
                elif not report.get("passed"):
                    trace_classification = classify_request_trace(observer.observations)
                    if trace_classification and report.get("failure_classification") in {
                        None,
                        "collaboration_e2e_incomplete",
                    }:
                        report["failure_classification"] = trace_classification
    if report.get("status") != "unverified":
        report["status"] = "passed" if report["passed"] else "failed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
