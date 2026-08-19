#!/usr/bin/env python3
"""Run bounded authenticated Codex CLI qualification against Ollama Cloud.

The live credential is resolved from an installed CodexHub provider file and is
passed only to catalog-sync/Gateway children through a dedicated environment
variable.  Evidence retains shapes, counts, statuses, Provider/model/protocol
identity, and hashes; it never retains prompts, response text, request bodies,
paths, opaque runtime identifiers, or credentials.
"""
from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib
from typing import Any, Iterable, Iterator, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_issue_283_cli_v2_lifecycle as lifecycle
from collaboration_runtime_contract import V2_TOOLS

SCHEMA = "codexhub.authenticated-provider-cli.v1"
PROVIDER_ID = "ollama-cloud"
QUALIFICATION_KEY_ENV = "CODEXHUB_AUTH_QUAL_KEY"
DEFAULT_CREDENTIAL_SOURCE = Path.home() / ".codex" / "proxy" / "config" / "providers.toml"
DEFAULT_OUTPUT_DIR = Path("docs/evidence/authenticated-provider")
EXPECTED_V2_TOOLS = frozenset(V2_TOOLS)
EXPECTED_V2_SEQUENCE = (
    "spawn_agent",
    "wait_agent",
    "followup_task",
    "send_message",
    "list_agents",
    "interrupt_agent",
)
SENTINELS = {
    "identity_text": "AUTH_PROVIDER_TEXT_OK",
    "identity_resume": "AUTH_PROVIDER_TEXT_RESUME_OK",
    "file_workflow": "AUTH_PROVIDER_PATCH_OK",
    "collaboration_phase1": "AUTH_PROVIDER_V2_PHASE1_OK",
    "collaboration_followup": "AUTH_PROVIDER_V2_FOLLOWUP_OK",
    "collaboration_message": "AUTH_PROVIDER_V2_MESSAGE_OK",
    "collaboration_list": "AUTH_PROVIDER_V2_LIST_OK",
    "collaboration": "AUTH_PROVIDER_V2_OK",
    "resume": "AUTH_PROVIDER_RESUME_OK",
}


@dataclass(frozen=True)
class ModelFacts:
    model: str
    display_name: str
    context_window: int
    max_output_tokens: int


@dataclass(frozen=True)
class Cell:
    protocol: str
    model: ModelFacts

    @property
    def key(self) -> str:
        return f"{self.protocol}:{self.model.model}"

    @property
    def cli_model(self) -> str:
        return f"{PROVIDER_ID}/{self.model.model}"


MODELS = (
    ModelFacts("glm-5.2", "Ollama GLM-5.2", 1_000_000, 131_072),
    ModelFacts("kimi-k2.7-code", "Ollama Kimi K2.7 Code", 262_144, 32_768),
    ModelFacts("deepseek-v4-flash:0731", "Ollama DeepSeek V4 Flash 0731", 1_048_576, 393_216),
)
PROTOCOLS = ("responses", "chat_completions")
CELLS = tuple(Cell(protocol, model) for protocol in PROTOCOLS for model in MODELS)
CELL_BY_KEY = {cell.key: cell for cell in CELLS}
SCENARIOS = ("identity_text", "file_workflow", "collaboration")


class QualificationFailure(RuntimeError):
    """Fixed-code qualification failure safe for bounded summaries."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise QualificationFailure(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_failure_code(error: BaseException) -> str:
    if isinstance(error, QualificationFailure):
        return str(error).split(":", 1)[0][:80]
    if isinstance(error, lifecycle.CaptureFailure):
        return str(error).split(":", 1)[0][:80]
    return type(error).__name__


def _credential_provider(path: Path) -> tuple[str, str]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise QualificationFailure("credential_source_invalid") from error
    matches = [
        provider
        for provider in document.get("providers", [])
        if isinstance(provider, Mapping) and provider.get("id") == PROVIDER_ID
    ]
    _require(len(matches) == 1, "credential_provider_missing")
    provider = matches[0]
    key = provider.get("api_key")
    base_url = provider.get("base_url")
    _require(isinstance(key, str) and bool(key.strip()), "credential_missing")
    _require(not key.startswith("{env:"), "credential_unresolved")
    _require(isinstance(base_url, str) and base_url.startswith("https://"), "credential_base_url_invalid")
    return key, base_url.rstrip("/")


@contextmanager
def _qualification_secret(value: str) -> Iterator[None]:
    previous = os.environ.get(QUALIFICATION_KEY_ENV)
    os.environ[QUALIFICATION_KEY_ENV] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(QUALIFICATION_KEY_ENV, None)
        else:
            os.environ[QUALIFICATION_KEY_ENV] = previous


def _provider_config(cell: Cell, base_url: str) -> str:
    if cell.protocol == "chat_completions":
        protocol_lines = """upstream_format = "chat_completions"
available_upstream_formats = ["chat_completions"]
tool_protocol = "chat_tools"
"""
        capabilities = """namespace_lifecycle = false
function_lifecycle = true
custom_lifecycle = false
tool_search_lifecycle = false
accepts_namespace_adapter = true
accepts_custom_adapter = true
accepts_tool_search_adapter = true
"""
    else:
        protocol_lines = """upstream_format = "responses"
available_upstream_formats = ["responses"]
tool_protocol = "responses_structured"
"""
        capabilities = """namespace_lifecycle = true
function_lifecycle = true
custom_lifecycle = true
tool_search_lifecycle = true
accepts_namespace_adapter = false
accepts_custom_adapter = false
accepts_tool_search_adapter = false
"""
    if cell.protocol == "responses":
        model_codec = "strict_apply_patch" if cell.model.model == "glm-5.2" else "none"
        model_strategy = "deferred_core" if cell.model.model == "glm-5.2" else "eager"
        model_overrides = (
            f'  tool_surface_strategy = "{model_strategy}"' + chr(10)
            + f'  native_responses_tool_codec = "{model_codec}"' + chr(10)
        )
    else:
        # Exercise bundled maintained model defaults.  The native Responses
        # codec may still be configured for the same model, but production
        # must scope it away from a Chat attempt.
        model_overrides = ""
    return f'''[[providers]]
id = "{PROVIDER_ID}"
name = "Ollama Cloud Qualification"
base_url = {json.dumps(base_url)}
api_key = "{{env:{QUALIFICATION_KEY_ENV}}}"
{protocol_lines}display_prefix = "Ollama"
sort_order = 1
enabled = true

[providers.tool_protocol_capabilities]
{capabilities}
  [[providers.models]]
  id = {json.dumps(cell.model.model)}
  display_name = {json.dumps(cell.model.display_name)}
  context_window = {cell.model.context_window}
  max_output_tokens = {cell.model.max_output_tokens}
{model_overrides}  multi_agent_version = "v2"
  sort_order = 1
  enabled = true
'''


def _write_provider_config(home: Path, cell: Cell, base_url: str) -> Path:
    path = home / "proxy" / "config" / "providers.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_provider_config(cell, base_url), encoding="utf-8", newline=chr(10))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _sync_catalog(home: Path, secret: str) -> None:
    with _qualification_secret(secret):
        lifecycle._sync_catalog(home)


def _start_gateway(home: Path, port: int, secret: str) -> subprocess.Popen[str]:
    with _qualification_secret(secret):
        return lifecycle._start_gateway(home, port)


def _probe_upstream(cell: Cell, base_url: str, secret: str) -> dict[str, Any]:
    endpoint = "responses" if cell.protocol == "responses" else "chat/completions"
    body: dict[str, Any] = {"model": cell.model.model, "stream": False}
    if cell.protocol == "responses":
        body.update({"input": "Reply with exactly OK.", "max_output_tokens": 32})
    else:
        body.update({"messages": [{"role": "user", "content": "Reply with exactly OK."}], "max_tokens": 32})
    request = Request(
        f"{base_url}/{endpoint}",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
    )
    started = time.monotonic()
    result: dict[str, Any] = {"endpoint": endpoint, "stream": False}
    try:
        with urlopen(request, timeout=240) as response:
            payload = json.load(response)
            result["http_status"] = int(response.status)
        result["status"] = "passed"
        result["object"] = payload.get("object") if isinstance(payload, Mapping) else None
        result["has_output"] = isinstance(payload, Mapping) and isinstance(payload.get("output"), list)
        result["has_choices"] = isinstance(payload, Mapping) and isinstance(payload.get("choices"), list)
    except HTTPError as error:
        result.update(status="http_error", http_status=int(error.code))
    except Exception as error:
        result.update(status="transport_error", error_type=type(error).__name__)
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def _terminal_error_classification(lines: Iterable[str]) -> dict[str, Any] | None:
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping) or value.get("type") not in {"turn.failed", "error"}:
            continue
        message = value.get("message")
        if not isinstance(message, str):
            continue
        try:
            envelope = json.loads(message)
        except json.JSONDecodeError:
            return {"classification": "unstructured_cli_error"}
        if not isinstance(envelope, Mapping):
            return {"classification": "unstructured_cli_error"}
        codexhub = envelope.get("codexhub_error")
        details = codexhub.get("details") if isinstance(codexhub, Mapping) else None
        if isinstance(details, Mapping):
            return {
                "classification": details.get("error") or details.get("type"),
                "status": details.get("status"),
                "failure_class": details.get("failure_class"),
            }
        return {"classification": "unclassified_cli_error"}
    return None


def _item_types(lines: Iterable[str]) -> list[str]:
    types: set[str] = set()
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = value.get("item") if isinstance(value, Mapping) else None
        if isinstance(item, Mapping) and isinstance(item.get("type"), str):
            types.add(item["type"])
    return sorted(types)


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _session_items(home: Path) -> Iterator[Mapping[str, Any]]:
    for path in home.rglob("*.jsonl"):
        if "sessions" not in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield from _walk(value)


def _session_tool_evidence(home: Path) -> dict[str, Any]:
    calls: dict[str, tuple[str, str | None]] = {}
    call_order: list[str] = []
    outputs: dict[str, Any] = {}
    for item in _session_items(home):
        item_type = item.get("type")
        if item_type in {"function_call", "custom_tool_call"}:
            call_id = item.get("call_id")
            name = item.get("name")
            namespace = item.get("namespace")
            if isinstance(call_id, str) and isinstance(name, str) and call_id not in calls:
                calls[call_id] = (name, namespace if isinstance(namespace, str) else None)
                call_order.append(call_id)
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id not in outputs:
                outputs[call_id] = item.get("output")

    def lifecycle(name: str, namespace: str | None = None) -> tuple[list[str], bool]:
        matching = [
            call_id
            for call_id in call_order
            if calls[call_id] == (name, namespace)
            or (namespace is None and calls[call_id][0] == name)
        ]
        return matching, bool(matching) and all(call_id in outputs for call_id in matching)

    exec_calls, exec_identity = lifecycle("exec_command")
    patch_calls, patch_identity = lifecycle("apply_patch")
    collaboration_ids = [
        call_id for call_id in call_order if calls[call_id][1] == "collaboration"
    ]
    raw_sequence = [calls[call_id][0] for call_id in collaboration_ids]
    collaboration_sequence = list(dict.fromkeys(raw_sequence))
    results_by_name: dict[str, list[Any]] = {}
    for call_id in collaboration_ids:
        if call_id in outputs:
            results_by_name.setdefault(calls[call_id][0], []).append(outputs[call_id])

    spawn_results = results_by_name.get("spawn_agent", [])
    spawn_objects: list[Mapping[str, Any]] = []
    for value in spawn_results:
        if not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            spawn_objects.append(parsed)
    canonical_task = any(
        isinstance(result.get("task_name"), str)
        and result["task_name"].startswith("/root/")
        for result in spawn_objects
    )

    wait_outputs = results_by_name.get("wait_agent", [])
    list_outputs = results_by_name.get("list_agents", [])
    interrupt_outputs = results_by_name.get("interrupt_agent", [])
    def parsed_object(value: Any) -> Mapping[str, Any] | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, Mapping) else None

    def object_keys(value: Any) -> set[str]:
        parsed = parsed_object(value)
        return set(parsed) if parsed is not None else set()

    wait_objects = [parsed_object(value) for value in wait_outputs]
    wait_result_shape_valid = any(
        result is not None
        and set(result) == {"message", "timed_out"}
        and isinstance(result.get("message"), str)
        and isinstance(result.get("timed_out"), bool)
        for result in wait_objects
    )
    child_delivery = any(
        result is not None
        and result.get("timed_out") is False
        and isinstance(result.get("message"), str)
        for result in wait_objects
    )
    list_status_valid = False
    for value in list_outputs:
        result = parsed_object(value)
        agents = result.get("agents") if result is not None else None
        if (
            result is not None
            and set(result) == {"agents"}
            and isinstance(agents, list)
            and bool(agents)
            and all(
                isinstance(agent, Mapping)
                and isinstance(agent.get("agent_name"), str)
                and isinstance(agent.get("agent_status"), str)
                for agent in agents
            )
        ):
            list_status_valid = True
    interrupt_result_shape_valid = any(
        object_keys(value) == {"previous_status"}
        and isinstance(parsed_object(value).get("previous_status"), str)
        for value in interrupt_outputs
        if parsed_object(value) is not None
    )

    return {
        "exec_command_call_count": len(exec_calls),
        "exec_command_identity_preserved": exec_identity,
        "apply_patch_call_count": len(patch_calls),
        "apply_patch_identity_preserved": patch_identity,
        "collaboration_sequence": collaboration_sequence,
        "collaboration_call_count": len(collaboration_ids),
        "collaboration_history_identity_preserved": bool(collaboration_ids)
        and all(call_id in outputs for call_id in collaboration_ids),
        "canonical_task_identity_observed": canonical_task,
        "child_result_delivery_observed": child_delivery,
        "wait_result_shape_valid": wait_result_shape_valid,
        "list_result_shape_valid": list_status_valid,
        "interrupt_result_shape_valid": interrupt_result_shape_valid,
    }


def _session_collaboration_counts(home: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for item in _session_items(home):
        if (
            item.get("type") == "function_call"
            and item.get("namespace") == "collaboration"
            and isinstance(item.get("name"), str)
        ):
            counts[item["name"]] += 1
    return counts


def _session_agent_message_count(home: Path) -> int:
    count = 0
    count += sum(
        1
        for item in _session_items(home)
        if item.get("type") == "agent_message"
        and isinstance(item.get("author"), str)
        and isinstance(item.get("recipient"), str)
    )
    return count


def _request_start_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and value.get("event") == "request_start":
            count += 1
    return count


def _gateway_summary(path: Path) -> dict[str, Any]:
    providers: set[str] = set()
    models: set[str] = set()
    protocols: set[str] = set()
    failures: list[dict[str, Any]] = []
    event_types: Counter[str] = Counter()
    reasoning_policies: set[str] = set()
    stream_chunk_counts: list[int] = []
    text_delta_sources = 0
    progressive_text_streams = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping):
                continue
            name = event.get("event")
            if isinstance(name, str):
                event_types[name] += 1
            if name == "request_start":
                if isinstance(event.get("route_provider_id"), str):
                    providers.add(event["route_provider_id"])
                if isinstance(event.get("route_upstream_model"), str):
                    models.add(event["route_upstream_model"])
                if isinstance(event.get("executed_upstream_protocol"), str):
                    protocols.add(event["executed_upstream_protocol"])
                if isinstance(event.get("reasoning_policy"), str):
                    reasoning_policies.add(event["reasoning_policy"])
            if name == "chat_stream_shape_summary":
                chunk_count = event.get("chunk_count")
                delta_sources = event.get("delta_source_count")
                text_chars = event.get("text_chars")
                if isinstance(chunk_count, int):
                    stream_chunk_counts.append(chunk_count)
                if isinstance(delta_sources, int):
                    text_delta_sources += delta_sources
                if (
                    isinstance(chunk_count, int)
                    and chunk_count >= 3
                    and isinstance(delta_sources, int)
                    and delta_sources >= 2
                    and isinstance(text_chars, int)
                    and text_chars > 0
                ):
                    progressive_text_streams += 1
            if name in {"request_error", "downstream_stream_closed"} or (
                name == "request_complete" and event.get("status") != 200
            ):
                bounded = {
                    key: event.get(key)
                    for key in ("event", "status", "failure_class", "failure_phase", "retry_safety")
                    if event.get(key) is not None
                }
                if event.get("failure_phase") in {"connect", "tls_handshake", "upstream_open"}:
                    bounded["category"] = "upstream_transport_or_availability"
                elif event.get("failure_phase") == "downstream_write" or event.get("status") == 499:
                    bounded["category"] = "client_cancellation"
                elif event.get("status") == 400:
                    bounded["category"] = "request_or_semantic_rejection"
                elif isinstance(event.get("status"), int) and event["status"] >= 500:
                    bounded["category"] = "upstream_or_gateway_failure"
                else:
                    bounded["category"] = "other_non_success"
                failures.append(bounded)
    analyzed = lifecycle._analyze_gateway_log(path)
    return {
        "providers": sorted(providers),
        "models": sorted(models),
        "protocols": sorted(protocols),
        "event_types": dict(sorted(event_types.items())),
        "reasoning_policies": sorted(reasoning_policies),
        "streaming": {
            "summary_count": len(stream_chunk_counts),
            "max_chunk_count": max(stream_chunk_counts, default=0),
            "text_delta_source_count": text_delta_sources,
            "progressive_text_stream_count": progressive_text_streams,
        },
        "failures": failures[:64],
        "fallback_count": analyzed.get("fallback_count", 0),
        "has_v1_observation": analyzed.get("has_v1_observation", False),
        "has_v2_observation": analyzed.get("has_v2_observation", False),
    }


def _run_identity(home: Path, port: int, workspace: Path) -> dict[str, Any]:
    code, lines, stdout, _stderr = lifecycle._run_cli(
        home,
        port,
        f"Reply with exactly {SENTINELS['identity_text']} and do not call tools.",
        workspace,
    )
    analysis = lifecycle._analyze_cli_events(lines)
    session_id = lifecycle._thread_id_from_cli_lines(lines)
    resume_code = -1
    resume_terminal: str | None = None
    resume_sentinel = False
    resume_item_types: list[str] = []
    if session_id:
        resume_code, resume_lines, resume_stdout, _resume_stderr = lifecycle._run_cli_resume(
            home,
            session_id,
            "Continue this same text-only conversation. Do not call tools. Reply with exactly "
            + SENTINELS["identity_resume"] + ".",
            workspace,
        )
        resume_analysis = lifecycle._analyze_cli_events(resume_lines)
        resume_terminal = resume_analysis.get("terminal_event")
        resume_sentinel = SENTINELS["identity_resume"] in resume_stdout
        resume_item_types = _item_types(resume_lines)
    multi_turn = (
        bool(session_id)
        and resume_code == 0
        and resume_terminal == "turn.completed"
        and resume_sentinel
        and resume_item_types == ["agent_message"]
    )
    passed = (
        code == 0
        and analysis.get("terminal_event") == "turn.completed"
        and SENTINELS["identity_text"] in stdout
        and _item_types(lines) == ["agent_message"]
        and multi_turn
    )
    return {
        "status": "passed" if passed else "failed",
        "exit_code": code,
        "terminal_event": analysis.get("terminal_event"),
        "sentinel_observed": SENTINELS["identity_text"] in stdout,
        "item_types": _item_types(lines),
        "multi_turn_history": {
            "passed": multi_turn,
            "resume_exit_code": resume_code,
            "resume_terminal_event": resume_terminal,
            "resume_sentinel_observed": resume_sentinel,
            "resume_item_types": resume_item_types,
        },
    }


def _run_file_workflow(home: Path, port: int, workspace: Path) -> dict[str, Any]:
    target = workspace / "provider-target.txt"
    target.write_text("alpha" + chr(10), encoding="utf-8", newline=chr(10))
    exact_patch = chr(10).join(
        (
            "*** Begin Patch",
            "*** Update File: provider-target.txt",
            "@@",
            "-alpha",
            "+beta",
            "*** End Patch",
        )
    )
    prompt = (
        "First use exec_command only to read provider-target.txt. Then you MUST call the "
        "custom file-editing tool whose description says Apply a patch; its wire name may be "
        "opaque and its required JSON argument is __codexhub_custom_input. Use this exact "
        f"string value without markdown fences: {exact_patch}. Do not edit through exec_command, "
        "scripts, sed, or shell redirection. Finally use exec_command only to verify the exact "
        f"file content and reply {SENTINELS['file_workflow']}."
    )
    code, lines, stdout, _stderr = lifecycle._run_cli(home, port, prompt, workspace)
    analysis = lifecycle._analyze_cli_events(lines)
    item_types = _item_types(lines)
    file_verified = target.read_text(encoding="utf-8") == "beta" + chr(10)
    tool_evidence = _session_tool_evidence(home)
    passed = (
        code == 0
        and analysis.get("terminal_event") == "turn.completed"
        and SENTINELS["file_workflow"] in stdout
        and file_verified
        and "command_execution" in item_types
        and "file_change" in item_types
        and tool_evidence["exec_command_call_count"] >= 2
        and tool_evidence["exec_command_identity_preserved"]
        and tool_evidence["apply_patch_call_count"] >= 1
        and tool_evidence["apply_patch_identity_preserved"]
    )
    return {
        "status": "passed" if passed else "failed",
        "exit_code": code,
        "terminal_event": analysis.get("terminal_event"),
        "sentinel_observed": SENTINELS["file_workflow"] in stdout,
        "file_verified": file_verified,
        "item_types": item_types,
        "standard_function_lifecycle": {
            "call_count": tool_evidence["exec_command_call_count"],
            "exact_call_result_identity": tool_evidence["exec_command_identity_preserved"],
        },
        "custom_tool_lifecycle": {
            "call_count": tool_evidence["apply_patch_call_count"],
            "exact_call_result_identity": tool_evidence["apply_patch_identity_preserved"],
        },
        "terminal_error": _terminal_error_classification(lines),
    }


def _run_collaboration(
    home: Path,
    port: int,
    workspace: Path,
    secret: str,
    base_url: str,
    cell: Cell,
    process: subprocess.Popen[str],
) -> tuple[dict[str, Any], subprocess.Popen[str]]:
    agents_path = workspace / "AGENTS.md"
    agents_path.write_text("# Provider qualification sentinel" + chr(10), encoding="utf-8", newline=chr(10))
    config_path = home / "config.toml"
    config_before = _sha256_file(config_path)
    agents_before = _sha256_file(agents_path)
    phase1_prompt = (
        "Use collaboration spawn_agent to spawn provider_worker with task: reply CHILD_DONE "
        "exactly and do not call tools. Then call wait_agent with timeout_ms 30000 until that "
        f"child result arrives. Reply {SENTINELS['collaboration_phase1']}."
    )
    phase1_code, phase1_lines, phase1_stdout, _phase1_stderr = lifecycle._run_cli(
        home, port, phase1_prompt, workspace
    )
    phase1_analysis = lifecycle._analyze_cli_events(phase1_lines)
    session_id = lifecycle._thread_id_from_cli_lines(phase1_lines)
    code = -1
    lines: list[str] = []
    stdout = ""
    analysis: dict[str, Any] = {}
    lifecycle_phases: dict[str, dict[str, Any]] = {}
    phase_specs = (
        (
            "followup_task",
            "Call collaboration followup_task on /root/provider_worker with task: run "
            "exec_command sleep 90 and then reply FOLLOWUP_DONE. Do not call other "
            "collaboration tools. Reply " + SENTINELS["collaboration_followup"] + ".",
            SENTINELS["collaboration_followup"],
        ),
        (
            "send_message",
            "Call collaboration send_message to /root/provider_worker with ACK while the "
            "follow-up is active. Do not call other collaboration tools. Reply "
            + SENTINELS["collaboration_message"] + ".",
            SENTINELS["collaboration_message"],
        ),
        (
            "list_agents",
            "Call collaboration list_agents and observe the canonical task status. Do not "
            "call other collaboration tools. Reply " + SENTINELS["collaboration_list"] + ".",
            SENTINELS["collaboration_list"],
        ),
        (
            "interrupt_agent",
            "Call collaboration interrupt_agent on /root/provider_worker. Do not call other "
            "collaboration tools. Reply " + SENTINELS["collaboration"] + ".",
            SENTINELS["collaboration"],
        ),
    )
    if session_id:
        for target_tool, phase_prompt, phase_sentinel in phase_specs:
            before_counts = _session_collaboration_counts(home)
            code, lines, stdout, _stderr = lifecycle._run_cli_resume(
                home, session_id, phase_prompt, workspace
            )
            analysis = lifecycle._analyze_cli_events(lines)
            after_counts = _session_collaboration_counts(home)
            lifecycle_phases[target_tool] = {
                "exit_code": code,
                "terminal_event": analysis.get("terminal_event"),
                "sentinel_observed": phase_sentinel in stdout,
                "target_call_observed": after_counts.get(target_tool, 0)
                > before_counts.get(target_tool, 0),
            }
    counts_before_resume = _session_collaboration_counts(home)
    tool_evidence = _session_tool_evidence(home)
    observed = set(counts_before_resume)

    lifecycle._stop_gateway(process)
    process = _start_gateway(home, port, secret)
    request_log = home / "proxy" / "codex-proxy-events.jsonl"
    before_resume_requests = _request_start_count(request_log)
    resume_code = -1
    resume_terminal: str | None = None
    resume_sentinel = False
    if session_id:
        resume_code, resume_lines, resume_stdout, _resume_stderr = lifecycle._run_cli_resume(
            home,
            session_id,
            "Read the existing collaboration task state. Do not spawn another agent. "
            f"Reply with exactly {SENTINELS['resume']}.",
            workspace,
        )
        resume_analysis = lifecycle._analyze_cli_events(resume_lines)
        resume_terminal = resume_analysis.get("terminal_event")
        resume_sentinel = SENTINELS["resume"] in resume_stdout
    after_resume_requests = _request_start_count(request_log)
    counts_after_resume = _session_collaboration_counts(home)

    foreign_home = lifecycle._isolated_home()
    try:
        foreign_workspace = foreign_home / "workspace"
        foreign_workspace.mkdir()
        _write_provider_config(foreign_home, cell, base_url)
        lifecycle._write_cli_config(foreign_home, port)
        before_foreign = _request_start_count(request_log)
        foreign_code = -1
        if session_id:
            foreign_code, _foreign_lines, _foreign_stdout, _foreign_stderr = lifecycle._run_cli_resume(
                foreign_home,
                session_id,
                "This foreign Home must not read or mutate the parent task.",
                foreign_workspace,
            )
        after_foreign = _request_start_count(request_log)
        cross_home_rejected = bool(session_id) and foreign_code != 0 and after_foreign == before_foreign
    finally:
        lifecycle._remove_home(foreign_home)

    same_home = (
        bool(session_id)
        and resume_code == 0
        and resume_terminal == "turn.completed"
        and resume_sentinel
        and after_resume_requests > before_resume_requests
        and counts_after_resume.get("spawn_agent", 0) == counts_before_resume.get("spawn_agent", 0)
        and _sha256_file(config_path) == config_before
        and _sha256_file(agents_path) == agents_before
    )
    lifecycle_phases_pass = (
        set(lifecycle_phases) == {spec[0] for spec in phase_specs}
        and all(
            phase["exit_code"] == 0
            and phase["terminal_event"] == "turn.completed"
            and phase["sentinel_observed"]
            and phase["target_call_observed"]
            for phase in lifecycle_phases.values()
        )
    )
    passed = (
        phase1_code == 0
        and phase1_analysis.get("terminal_event") == "turn.completed"
        and SENTINELS["collaboration_phase1"] in phase1_stdout
        and code == 0
        and analysis.get("terminal_event") == "turn.completed"
        and SENTINELS["collaboration"] in stdout
        and lifecycle_phases_pass
        and observed == EXPECTED_V2_TOOLS
        and tool_evidence["collaboration_call_count"] >= len(EXPECTED_V2_SEQUENCE)
        and tool_evidence["collaboration_history_identity_preserved"]
        and tool_evidence["canonical_task_identity_observed"]
        and tool_evidence["child_result_delivery_observed"]
        and tool_evidence["wait_result_shape_valid"]
        and tool_evidence["list_result_shape_valid"]
        and tool_evidence["interrupt_result_shape_valid"]
        and same_home
        and cross_home_rejected
    )
    return (
        {
            "status": "passed" if passed else "failed",
            "exit_code": code,
            "terminal_event": analysis.get("terminal_event"),
            "sentinel_observed": SENTINELS["collaboration"] in stdout,
            "phase1": {
                "exit_code": phase1_code,
                "terminal_event": phase1_analysis.get("terminal_event"),
                "sentinel_observed": SENTINELS["collaboration_phase1"] in phase1_stdout,
            },
            "observed_tools": sorted(observed),
            "qualified_phase_sequence": list(EXPECTED_V2_SEQUENCE),
            "lifecycle_phases": lifecycle_phases,
            "observed_first_use_order": tool_evidence["collaboration_sequence"],
            "exact_call_result_history_identity": tool_evidence[
                "collaboration_history_identity_preserved"
            ],
            "canonical_task_identity_observed": tool_evidence[
                "canonical_task_identity_observed"
            ],
            "child_result_delivery_observed": tool_evidence[
                "child_result_delivery_observed"
            ],
            "result_shapes": {
                "wait": tool_evidence["wait_result_shape_valid"],
                "list_status": tool_evidence["list_result_shape_valid"],
                "interrupt": tool_evidence["interrupt_result_shape_valid"],
            },
            "same_home_restart": {
                "passed": same_home,
                "resume_exit_code": resume_code,
                "resume_terminal_event": resume_terminal,
                "resumed_request_count": after_resume_requests - before_resume_requests,
                "config_preserved": _sha256_file(config_path) == config_before,
                "agents_configuration_preserved": _sha256_file(agents_path) == agents_before,
                "no_new_spawn": counts_after_resume.get("spawn_agent", 0) == counts_before_resume.get("spawn_agent", 0),
            },
            "cross_home_rejected_before_gateway_request": cross_home_rejected,
        },
        process,
    )


def _run_cell(
    cell: Cell,
    scenarios: tuple[str, ...],
    secret: str,
    base_url: str,
) -> dict[str, Any]:
    home = lifecycle._isolated_home()
    process: subprocess.Popen[str] | None = None
    result: dict[str, Any] = {
        "cell": cell.key,
        "provider_id": PROVIDER_ID,
        "model": cell.model.model,
        "protocol": cell.protocol,
        "status": "failed",
        "scenarios": {},
    }
    try:
        provider_path = _write_provider_config(home, cell, base_url)
        _require(QUALIFICATION_KEY_ENV in provider_path.read_text(encoding="utf-8"), "provider_key_placeholder_missing")
        _require(secret not in provider_path.read_text(encoding="utf-8"), "provider_key_leaked")
        _sync_catalog(home, secret)
        port = lifecycle._free_port()
        process = _start_gateway(home, port, secret)
        lifecycle._write_cli_config(home, port)
        workspace = home / "workspace"
        workspace.mkdir()
        lifecycle.CLI_MODEL = cell.cli_model
        result["provider_probe"] = _probe_upstream(cell, base_url, secret)
        if "identity_text" in scenarios:
            result["scenarios"]["identity_text"] = _run_identity(home, port, workspace)
        if "file_workflow" in scenarios:
            result["scenarios"]["file_workflow"] = _run_file_workflow(home, port, workspace)
        if "collaboration" in scenarios:
            collaboration, process = _run_collaboration(
                home, port, workspace, secret, base_url, cell, process
            )
            result["scenarios"]["collaboration"] = collaboration
        gateway = _gateway_summary(home / "proxy" / "codex-proxy-events.jsonl")
        result["gateway"] = gateway
        expected_protocol = cell.protocol
        scenarios_pass = all(value.get("status") == "passed" for value in result["scenarios"].values())
        identity_bound = (
            gateway["providers"] == [PROVIDER_ID]
            and gateway["models"] == [cell.model.model]
            and gateway["protocols"] == [expected_protocol]
        )
        result["identity_bound"] = identity_bound
        protocol_observations = True
        if cell.protocol == "chat_completions":
            protocol_observations = (
                gateway["streaming"]["progressive_text_stream_count"] >= 1
                and gateway["streaming"]["text_delta_source_count"] >= 2
                and gateway["reasoning_policies"] == ["explicit"]
                and all(failure.get("category") for failure in gateway["failures"])
            )
            if "collaboration" in scenarios:
                protocol_observations = protocol_observations and (
                    gateway["event_types"].get("runtime_tool_adapter_response", 0)
                    >= len(EXPECTED_V2_SEQUENCE)
                )
        result["protocol_observations_passed"] = protocol_observations
        result["status"] = "passed" if (
            result["provider_probe"].get("status") == "passed"
            and scenarios_pass
            and identity_bound
            and protocol_observations
            and gateway["fallback_count"] == 0
            and not gateway["has_v1_observation"]
        ) else "failed"
        result["redacted_provider_config_sha256"] = _sha256_file(provider_path)
    except Exception as error:
        result["failure_code"] = _safe_failure_code(error)
    finally:
        if process is not None:
            lifecycle._stop_gateway(process)
        lifecycle._remove_home(home)
    return result


def _candidate_sha(value: str | None) -> str:
    if value:
        _require(bool(re.fullmatch(r"[0-9a-f]{40}", value)), "candidate_sha_invalid")
        return value
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    candidate = completed.stdout.strip()
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", candidate)), "candidate_sha_invalid")
    return candidate


def _cli_version() -> str:
    executable = shutil.which("codex")
    _require(executable is not None, "codex_executable_not_found")
    completed = subprocess.run(
        [str(Path(executable).resolve()), "--version"], capture_output=True, text=True, check=False
    )
    value = completed.stdout.strip()
    _require(completed.returncode == 0 and value.startswith("codex-cli "), "codex_version_invalid")
    return value


def capture(
    *,
    cells: tuple[Cell, ...],
    scenarios: tuple[str, ...],
    credential_source: Path,
    output_dir: Path,
    candidate_sha: str,
    cli_version: str,
) -> dict[str, Any]:
    secret, base_url = _credential_provider(credential_source)
    results = [_run_cell(cell, scenarios, secret, base_url) for cell in cells]
    summary = {
        "schema": SCHEMA,
        "candidate_sha": candidate_sha,
        "cli_version": cli_version,
        "provider_id": PROVIDER_ID,
        "cells": results,
        "qualification_status": "passed" if all(cell["status"] == "passed" for cell in results) else "failed",
        "sanitization": {
            "credentials_retained": False,
            "raw_prompts_retained": False,
            "raw_provider_output_retained": False,
            "request_bodies_retained": False,
            "absolute_paths_retained": False,
            "opaque_ids_retained": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10)
    )
    return summary


def _parse_cells(values: list[str] | None) -> tuple[Cell, ...]:
    if not values:
        return CELLS
    unknown = [value for value in values if value not in CELL_BY_KEY]
    _require(not unknown, "unknown_cell")
    return tuple(CELL_BY_KEY[value] for value in values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", action="append", choices=sorted(CELL_BY_KEY))
    parser.add_argument("--scenario", action="append", choices=SCENARIOS)
    parser.add_argument("--credential-source", type=Path, default=DEFAULT_CREDENTIAL_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--candidate-sha")
    args = parser.parse_args(argv)
    try:
        summary = capture(
            cells=_parse_cells(args.cell),
            scenarios=tuple(args.scenario or SCENARIOS),
            credential_source=args.credential_source,
            output_dir=args.output_dir,
            candidate_sha=_candidate_sha(args.candidate_sha),
            cli_version=_cli_version(),
        )
    except QualificationFailure as error:
        print(f"QUALIFICATION_FAILED:{_safe_failure_code(error)}", file=sys.stderr)
        return 2
    print(json.dumps({"schema": SCHEMA, "qualification_status": summary["qualification_status"]}))
    return 0 if summary["qualification_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
