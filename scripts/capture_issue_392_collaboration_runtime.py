#!/usr/bin/env python3
"""Capture the frozen #392 Collaboration contract from isolated runtimes.

This is an explicit evidence runner.  It accepts only the frozen CLI/Desktop
binaries and source commits, creates a new empty Codex Home for every
scenario, and talks only to a loopback protocol-controlled Responses server.
The emitted artifact contains structural types and relationship assertions;
it never retains prompts, credentials, paths, or opaque runtime identifiers.
"""

from __future__ import annotations

import argparse
import copy
from datetime import date
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_issue_392_collaboration_contract as contract
from run_issue_106_task_lifecycle import (
    JsonRpcClient,
    initialize,
    response_result,
    start_issue106_app_server,
    start_issue106_custom_thread,
    stop_issue106_app_server,
    turn_started,
    wait_for_turn,
)


SCHEMA = "codexhub.issue392.collaboration-runtime-observations.v1"
DEFAULT_OUTPUT = Path(
    "docs/evidence/issue-392/collaboration-runtime-observations.json"
)
HOME_PREFIX = "codexhub-issue392-runtime-"
MODEL = "fixture-v2"
CANDIDATE_REVISION = "be10f62f44b22fa8c84510238250ae11fb3ecab4"
SENSITIVE_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
)
DISABLED_FEATURES = ("plugins", "remote_plugin", "plugin_sharing")


class CaptureFailure(RuntimeError):
    """A fixed-code capture failure that never includes captured content."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CaptureFailure(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_text(command: Sequence[str], *, cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureFailure("frozen_input_command_failed") from error
    _require(completed.returncode == 0, "frozen_input_command_failed")
    return completed.stdout.strip()


def _git_value(source_root: Path, *arguments: str) -> str:
    return _run_text(("git", "-C", str(source_root), *arguments))


def _verify_runtime(
    *,
    client: str,
    executable: Path,
    source_root: Path,
    expected_binary_sha256: str,
    expected_runtime_version: str,
    expected_source_commit: str,
    expected_source_blobs: Mapping[str, str],
    client_version: str,
    source_tag: str,
) -> dict[str, Any]:
    _require(executable.is_file(), "frozen_binary_missing")
    _require(source_root.is_dir(), "frozen_source_missing")
    binary_sha256 = _sha256_file(executable)
    _require(binary_sha256 == expected_binary_sha256, "frozen_binary_hash_mismatch")
    version_output = _run_text((str(executable), "--version"))
    _require(
        version_output == f"codex-cli {expected_runtime_version}",
        "frozen_runtime_version_mismatch",
    )
    source_commit = _git_value(source_root, "rev-parse", "HEAD")
    _require(source_commit == expected_source_commit, "frozen_source_commit_mismatch")
    source_files: dict[str, dict[str, str]] = {}
    for key, relative_path in contract.SOURCE_FILES.items():
        blob = _git_value(
            source_root, "rev-parse", f"{expected_source_commit}:{relative_path}"
        )
        _require(blob == expected_source_blobs[key], "frozen_source_blob_mismatch")
        source_files[key] = {"path": relative_path, "git_blob": blob}
    return {
        "client": client,
        "client_version": client_version,
        "runtime_version": expected_runtime_version,
        "version_output": version_output,
        "binary_sha256": binary_sha256,
        "source_tag": source_tag,
        "source_commit": source_commit,
        "source_files": source_files,
    }


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "unknown"


def _field_types(value: Mapping[str, Any]) -> dict[str, str]:
    return {key: _value_type(child) for key, child in sorted(value.items())}


def _json_object_keys(value: Any) -> tuple[str, list[str]]:
    if not isinstance(value, str):
        return "not_string", []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return "plain_text", []
    if isinstance(decoded, Mapping):
        return "object", sorted(str(key) for key in decoded)
    return _value_type(decoded), []


def _declaration(body: Mapping[str, Any], namespace: str) -> dict[str, Any]:
    tools = body.get("tools")
    _require(isinstance(tools, list), "request_tools_invalid")
    matches = [
        tool
        for tool in tools
        if isinstance(tool, Mapping)
        and tool.get("type") == "namespace"
        and tool.get("name") == namespace
    ]
    _require(len(matches) == 1, "request_namespace_invalid")
    marker = matches[0]
    children = marker.get("tools")
    _require(isinstance(children, list), "request_namespace_children_invalid")
    normalized_children: list[dict[str, Any]] = []
    for child in children:
        _require(isinstance(child, Mapping), "request_namespace_child_invalid")
        parameters = child.get("parameters")
        _require(isinstance(parameters, Mapping), "request_namespace_schema_invalid")
        normalized_children.append(
            {
                "type": child.get("type"),
                "name": child.get("name"),
                "description_type": _value_type(child.get("description")),
                "strict": child.get("strict"),
                "fields": sorted(child),
                "parameters": contract._normalize_schema(parameters),
            }
        )
    normalized_children.sort(key=lambda value: str(value["name"]))
    return {
        "type": marker.get("type"),
        "name": marker.get("name"),
        "description_type": _value_type(marker.get("description")),
        "fields": sorted(marker),
        "children": normalized_children,
    }


def _version_locations(body: Mapping[str, Any]) -> list[str]:
    locations: list[str] = []
    if "multi_agent_version" in body:
        locations.append("request")
    for parent_name in ("metadata", "features", "client_metadata"):
        parent = body.get(parent_name)
        if isinstance(parent, Mapping) and "multi_agent_version" in parent:
            locations.append(parent_name)
    return locations


def _interesting_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    if item_type == "function_call":
        argument_kind, argument_keys = _json_object_keys(item.get("arguments"))
        return {
            "type": item_type,
            "fields": sorted(item),
            "field_types": _field_types(item),
            "name": item.get("name"),
            "namespace": item.get("namespace"),
            "arguments_json_type": argument_kind,
            "argument_keys": argument_keys,
        }
    if item_type == "function_call_output":
        output_kind, output_keys = _json_object_keys(item.get("output"))
        return {
            "type": item_type,
            "fields": sorted(item),
            "field_types": _field_types(item),
            "output_wire_type": _value_type(item.get("output")),
            "decoded_output_type": output_kind,
            "decoded_output_keys": output_keys,
        }
    if item_type == "agent_message":
        content = item.get("content")
        _require(isinstance(content, list), "agent_message_content_invalid")
        variants = []
        for part in content:
            _require(isinstance(part, Mapping), "agent_message_part_invalid")
            variants.append(
                {
                    "type": part.get("type"),
                    "fields": sorted(part),
                    "field_types": _field_types(part),
                }
            )
        return {
            "type": item_type,
            "fields": sorted(item),
            "field_types": _field_types(item),
            "content_variants": variants,
        }
    return None


def _collaboration_items(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = body.get("input")
    _require(isinstance(items, list), "request_input_invalid")
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            summary = _interesting_item(item)
            if summary is not None:
                result.append(summary)
    return result


def _input_type_order(body: Mapping[str, Any]) -> list[str]:
    items = body.get("input")
    _require(isinstance(items, list), "request_input_invalid")
    return [
        str(item.get("type"))
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("type"), str)
    ]


def _safe_request_shape(body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fields": sorted(body),
        "field_types": _field_types(body),
        "tool_choice": body.get("tool_choice"),
        "multi_agent_version_locations": _version_locations(body),
        "input_type_order": _input_type_order(body),
        "collaboration_items": _collaboration_items(body),
    }


def _sse(event: Mapping[str, Any]) -> bytes:
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    return (
        b"event: "
        + str(event["type"]).encode("utf-8")
        + b"\n"
        + b"data: "
        + body
        + b"\n\n"
    )


def _response_created(index: int) -> dict[str, Any]:
    return {
        "type": "response.created",
        "response": {
            "id": f"response_{index}",
            "object": "response",
            "status": "in_progress",
            "model": MODEL,
            "output": [],
        },
    }


def _response_completed(output: list[dict[str, Any]], index: int) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": f"response_{index}",
            "object": "response",
            "status": "completed",
            "model": MODEL,
            "output": output,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }


def _message_events(index: int) -> list[dict[str, Any]]:
    item_id = f"message_{index}"
    done = {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done", "annotations": []}],
    }
    return [
        _response_created(index),
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": "done",
        },
        {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "text": "done",
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        _response_completed([done], index),
    ]


def _function_events(
    name: str, arguments: Mapping[str, Any], index: int
) -> list[dict[str, Any]]:
    item_id = f"function_{index}"
    call_id = f"call_{index}"
    arguments_text = json.dumps(arguments, separators=(",", ":"))
    done = {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "name": name,
        "namespace": contract.V2_NAMESPACE,
        "call_id": call_id,
        "arguments": arguments_text,
    }
    added = {**done, "status": "in_progress", "arguments": ""}
    return [
        _response_created(index),
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": 0,
            "delta": arguments_text,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": 0,
            "arguments": arguments_text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        _response_completed([done], index),
    ]


class _FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, lifecycle: bool) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureHandler)
        self.lifecycle = lifecycle
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.root_thread: str | None = None
        self.root_request_count = 0

    def events_for(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        thread_id_value = (body.get("client_metadata") or {}).get("thread_id")
        thread_id = thread_id_value if isinstance(thread_id_value, str) else None
        with self.lock:
            self.requests.append(body)
            index = len(self.requests)
            if self.root_thread is None:
                self.root_thread = thread_id
            is_root = thread_id == self.root_thread
            if is_root:
                self.root_request_count += 1
                root_stage = self.root_request_count
            else:
                root_stage = 0
        if not self.lifecycle:
            return _message_events(index)
        if is_root and root_stage == 1:
            return _function_events(
                "spawn_agent",
                {"task_name": "worker", "message": "bounded child task"},
                index,
            )
        if is_root and root_stage == 2:
            return _function_events("wait_agent", {"timeout_ms": 10000}, index)
        return _message_events(index)


class _FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        server = self.server
        _require(isinstance(server, _FixtureServer), "fixture_server_invalid")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if not isinstance(body, dict):
            self.send_error(400)
            return
        events = server.events_for(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in events:
            self.wfile.write(_sse(event))
            self.wfile.flush()


class FixtureServer:
    def __init__(self, *, lifecycle: bool) -> None:
        self._server = _FixtureServer(lifecycle)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _FixtureServer:
        self._thread.start()
        return self._server

    def __exit__(self, *_args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class IsolatedHome:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.path: Path | None = None
        self.marker_sha256: str | None = None
        self.fresh_empty = False

    def __enter__(self) -> "IsolatedHome":
        path = Path(tempfile.mkdtemp(prefix=HOME_PREFIX))
        self.path = path
        self.fresh_empty = not any(path.iterdir())
        _require(self.fresh_empty, "isolated_home_not_empty")
        marker = self.scenario.encode("utf-8") + b"\0" + secrets.token_bytes(32)
        marker_path = path / ".codexhub-issue392-home-marker"
        marker_path.write_bytes(marker)
        self.marker_sha256 = hashlib.sha256(marker).hexdigest()
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.path is None:
            return
        last_error: OSError | None = None
        for _ in range(10):
            try:
                shutil.rmtree(self.path)
                return
            except FileNotFoundError:
                return
            except OSError as error:
                last_error = error
                time.sleep(0.25)
        raise CaptureFailure("isolated_home_cleanup_failed") from last_error


def _isolated_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment.update(
        CODEX_HOME=str(home),
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(home / "AppData/Roaming"),
        LOCALAPPDATA=str(home / "AppData/Local"),
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
        HTTP_PROXY="",
        HTTPS_PROXY="",
        ALL_PROXY="",
    )
    Path(environment["APPDATA"]).mkdir(parents=True)
    Path(environment["LOCALAPPDATA"]).mkdir(parents=True)
    return environment


def _write_config(home: Path, port: int, *, v2: bool, app_server: bool) -> None:
    provider = "custom" if app_server else "fixture"
    if v2:
        feature_config = """[features.multi_agent_v2]
enabled=true
non_code_mode_only=false
"""
    else:
        feature_config = """[features]
multi_agent=true
multi_agent_v2=false
"""
    (home / "config.toml").write_text(
        f'''model="{MODEL}"
model_provider="{provider}"
[model_providers.{provider}]
name="fixture"
base_url="http://127.0.0.1:{port}/v1"
wire_api="responses"
requires_openai_auth=false
experimental_bearer_token="fixture-key"
{feature_config}''',
        encoding="utf-8",
        newline="\n",
    )


def _exec_options(workspace: Path) -> list[str]:
    options = [
        "--json",
        "--skip-git-repo-check",
        "--strict-config",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    for feature in DISABLED_FEATURES:
        options.extend(("--disable", feature))
    options.extend(("-C", str(workspace), "-m", MODEL, "-"))
    return options


def _resume_options(session_id: str) -> list[str]:
    options = [session_id, "--json", "--strict-config"]
    for feature in DISABLED_FEATURES:
        options.extend(("--disable", feature))
    options.extend(
        (
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m",
            MODEL,
            "-",
        )
    )
    return options


def _run_codex(
    command: Sequence[str], *, prompt: str, cwd: Path, environment: Mapping[str, str]
) -> None:
    try:
        completed = subprocess.run(
            list(command),
            input=prompt,
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureFailure("frozen_runtime_execution_failed") from error
    _require(completed.returncode == 0, "frozen_runtime_execution_failed")


def _base_scenario(
    *, client: str, home: IsolatedHome, request_count: int, process_runs: int
) -> dict[str, Any]:
    _require(home.marker_sha256 is not None, "isolated_home_marker_missing")
    return {
        "client": client,
        "home_binding_sha256": home.marker_sha256,
        "fresh_empty_home_before_marker": home.fresh_empty,
        "workspace_created_under_home": True,
        "loopback_request_count": request_count,
        "process_runs": process_runs,
    }


def _capture_request_scenario(
    *, client: str, executable: Path, version: str
) -> dict[str, Any]:
    namespace = contract.V1_NAMESPACE if version == contract.V1 else contract.V2_NAMESPACE
    scenario_name = f"{client}_{version}_request"
    with IsolatedHome(scenario_name) as isolated:
        _require(isolated.path is not None, "isolated_home_missing")
        home = isolated.path
        workspace = home / "workspace"
        workspace.mkdir()
        with FixtureServer(lifecycle=False) as server:
            _write_config(home, server.server_port, v2=version == contract.V2, app_server=False)
            environment = _isolated_environment(home)
            _run_codex(
                (str(executable), "exec", *_exec_options(workspace)),
                prompt="Reply exactly done without using tools.",
                cwd=workspace,
                environment=environment,
            )
            requests = copy.deepcopy(server.requests)
        _require(len(requests) == 1, "request_scenario_count_invalid")
        request = requests[0]
        result = _base_scenario(
            client=client,
            home=isolated,
            request_count=len(requests),
            process_runs=1,
        )
        result["observed"] = {
            "request": _safe_request_shape(request),
            "declaration": _declaration(request, namespace),
        }
        return result


def _raw_collaboration_items(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inputs = body.get("input")
    _require(isinstance(inputs, list), "request_input_invalid")
    return [
        item
        for item in inputs
        if isinstance(item, Mapping)
        and item.get("type")
        in {"function_call", "function_call_output", "agent_message"}
    ]


def _phase_requests(
    requests: Sequence[Mapping[str, Any]], root_thread: str, restart_offset: int
) -> dict[str, Mapping[str, Any]]:
    phases: dict[str, Mapping[str, Any]] = {}
    child_count = 0
    root_count = 0
    for index, body in enumerate(requests):
        thread_id = (body.get("client_metadata") or {}).get("thread_id")
        if thread_id == root_thread:
            root_count += 1
            if index >= restart_offset:
                phase = "restart_replay"
            elif root_count == 1:
                phase = "root_initial"
            elif root_count == 2:
                phase = "root_after_spawn"
            else:
                phase = "root_after_wait"
        else:
            child_count += 1
            phase = "child_initial" if child_count == 1 else f"child_{child_count}"
        _require(phase not in phases, "lifecycle_phase_duplicate")
        phases[phase] = body
    _require(
        set(phases)
        == {
            "root_initial",
            "root_after_spawn",
            "child_initial",
            "root_after_wait",
            "restart_replay",
        },
        "lifecycle_phase_set_invalid",
    )
    return phases


def _item_signature(item: Mapping[str, Any]) -> tuple[Any, ...]:
    item_type = item.get("type")
    if item_type == "function_call":
        return (item_type, item.get("name"), item.get("namespace"))
    if item_type == "agent_message":
        content = item.get("content")
        _require(isinstance(content, list), "agent_message_content_invalid")
        kinds = tuple(
            part.get("type")
            for part in content
            if isinstance(part, Mapping)
        )
        return (item_type, kinds)
    return (item_type,)


def _identity_relationships(
    phases: Mapping[str, Mapping[str, Any]]
) -> dict[str, bool]:
    before = _raw_collaboration_items(phases["root_after_wait"])
    replay = _raw_collaboration_items(phases["restart_replay"])
    before_calls = [item for item in before if item.get("type") == "function_call"]
    replay_calls = [item for item in replay if item.get("type") == "function_call"]
    before_outputs = [
        item for item in before if item.get("type") == "function_call_output"
    ]
    replay_outputs = [
        item for item in replay if item.get("type") == "function_call_output"
    ]
    before_agents = [item for item in before if item.get("type") == "agent_message"]
    replay_agents = [item for item in replay if item.get("type") == "agent_message"]
    call_output_links = all(
        any(output.get("call_id") == call.get("call_id") for output in before_outputs)
        for call in before_calls
    )
    return {
        "function_call_item_ids_preserved": [item.get("id") for item in before_calls]
        == [item.get("id") for item in replay_calls],
        "function_call_call_ids_preserved": [
            item.get("call_id") for item in before_calls
        ]
        == [item.get("call_id") for item in replay_calls],
        "function_output_item_ids_preserved": [
            item.get("id") for item in before_outputs
        ]
        == [item.get("id") for item in replay_outputs],
        "function_output_call_ids_preserved": [
            item.get("call_id") for item in before_outputs
        ]
        == [item.get("call_id") for item in replay_outputs],
        "function_outputs_link_to_calls": call_output_links,
        "collaboration_item_order_preserved": [
            _item_signature(item) for item in before
        ]
        == [_item_signature(item) for item in replay],
        "agent_message_author_recipient_and_content_preserved": [
            (
                item.get("author"),
                item.get("recipient"),
                [
                    part.get("type")
                    for part in item.get("content", [])
                    if isinstance(part, Mapping)
                ],
            )
            for item in before_agents
        ]
        == [
            (
                item.get("author"),
                item.get("recipient"),
                [
                    part.get("type")
                    for part in item.get("content", [])
                    if isinstance(part, Mapping)
                ],
            )
            for item in replay_agents
        ],
    }


def _rollout_summary(home: Path, root_thread: str) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for path in sorted(home.rglob("*.jsonl")):
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        session_id = None
        for record in records:
            if record.get("type") == "session_meta" and isinstance(
                record.get("payload"), Mapping
            ):
                session_id = record["payload"].get("id")
                break
        role = "root" if session_id == root_thread else "child"
        relevant = [
            record
            for record in records
            if record.get("type") in {"session_meta", "turn_context", "response_item"}
        ]
        metadata: list[dict[str, Any]] = []
        collaboration_items: list[dict[str, Any]] = []
        for record in relevant:
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            record_type = record.get("type")
            if record_type in {"session_meta", "turn_context"}:
                metadata.append(
                    {
                        "record_type": record_type,
                        "record_fields": sorted(record),
                        "record_field_types": _field_types(record),
                        "payload_fields": sorted(payload),
                        "payload_field_types": _field_types(payload),
                        "multi_agent_version": payload.get("multi_agent_version"),
                    }
                )
            elif record_type == "response_item":
                item = _interesting_item(payload)
                if item is not None:
                    collaboration_items.append(item)
        summaries.append(
            {
                "role": role,
                "relevant_record_type_order": [record.get("type") for record in relevant],
                "metadata_records": metadata,
                "collaboration_items": collaboration_items,
            }
        )
    summaries.sort(key=lambda value: (value["role"] != "root", value["role"]))
    return summaries


def _capture_v2_lifecycle(
    *, client: str, executable: Path
) -> dict[str, Any]:
    scenario_name = f"{client}_collaboration_v2_lifecycle"
    with IsolatedHome(scenario_name) as isolated:
        _require(isolated.path is not None, "isolated_home_missing")
        home = isolated.path
        workspace = home / "workspace"
        workspace.mkdir()
        with FixtureServer(lifecycle=True) as server:
            _write_config(home, server.server_port, v2=True, app_server=False)
            environment = _isolated_environment(home)
            _run_codex(
                (str(executable), "exec", *_exec_options(workspace)),
                prompt="Spawn worker, wait for completion, then finish.",
                cwd=workspace,
                environment=environment,
            )
            _require(isinstance(server.root_thread, str), "root_thread_missing")
            root_thread = server.root_thread
            restart_offset = len(server.requests)
            _run_codex(
                (str(executable), "exec", "resume", *_resume_options(root_thread)),
                prompt="Reply exactly done again without using tools.",
                cwd=workspace,
                environment=environment,
            )
            requests = copy.deepcopy(server.requests)
        phases = _phase_requests(requests, root_thread, restart_offset)
        declaration = _declaration(phases["root_initial"], contract.V2_NAMESPACE)
        rollout = _rollout_summary(home, root_thread)
        result = _base_scenario(
            client=client,
            home=isolated,
            request_count=len(requests),
            process_runs=2,
        )
        result["observed"] = {
            "declaration": declaration,
            "request_arrival_order": [
                phase
                for body in requests
                for phase, phase_body in phases.items()
                if phase_body is body
            ],
            "requests_by_phase": {
                phase: _safe_request_shape(body) for phase, body in sorted(phases.items())
            },
            "served_function_call_event_order": [
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
            "served_function_names": ["spawn_agent", "wait_agent"],
            "identity_relationships": _identity_relationships(phases),
            "rollout_readback": rollout,
        }
        return result


def _notification_summary(notification: Mapping[str, Any]) -> dict[str, Any] | None:
    method = notification.get("method")
    params = notification.get("params")
    if not isinstance(method, str) or not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if isinstance(item, Mapping) and item.get("type") in {
        "agentMessage",
        "collabAgentToolCall",
        "subAgentActivity",
    }:
        result: dict[str, Any] = {
            "method": method,
            "params_fields": sorted(params),
            "params_field_types": _field_types(params),
            "item_type": item.get("type"),
            "item_fields": sorted(item),
            "item_field_types": _field_types(item),
        }
        for field in ("kind", "status", "tool", "phase"):
            value = item.get(field)
            if isinstance(value, str):
                result[f"item_{field}"] = value
        return result
    if method == "item/agentMessage/delta":
        return {
            "method": method,
            "params_fields": sorted(params),
            "params_field_types": _field_types(params),
        }
    return None


def _thread_structure(thread: Mapping[str, Any]) -> dict[str, Any]:
    turns = thread.get("turns")
    _require(isinstance(turns, list), "desktop_thread_turns_invalid")
    turn_summaries: list[dict[str, Any]] = []
    for turn in turns:
        _require(isinstance(turn, Mapping), "desktop_turn_invalid")
        items = turn.get("items")
        _require(isinstance(items, list), "desktop_turn_items_invalid")
        item_summaries: list[dict[str, Any]] = []
        for item in items:
            _require(isinstance(item, Mapping), "desktop_thread_item_invalid")
            summary: dict[str, Any] = {
                "type": item.get("type"),
                "fields": sorted(item),
                "field_types": _field_types(item),
            }
            for field in ("kind", "status", "tool", "phase"):
                value = item.get(field)
                if isinstance(value, str):
                    summary[field] = value
            item_summaries.append(summary)
        turn_summaries.append(
            {
                "fields": sorted(turn),
                "field_types": _field_types(turn),
                "status": turn.get("status"),
                "items": item_summaries,
            }
        )
    status = thread.get("status")
    return {
        "fields": sorted(thread),
        "field_types": _field_types(thread),
        "status_fields": sorted(status) if isinstance(status, Mapping) else [],
        "status_field_types": _field_types(status) if isinstance(status, Mapping) else {},
        "turns": turn_summaries,
    }


def _desktop_identity_relationships(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, bool]:
    before_turns = before.get("turns")
    after_turns = after.get("turns")
    _require(isinstance(before_turns, list), "desktop_thread_turns_invalid")
    _require(isinstance(after_turns, list), "desktop_thread_turns_invalid")
    before_item_ids = [
        [item.get("id") for item in turn.get("items", []) if isinstance(item, Mapping)]
        for turn in before_turns
        if isinstance(turn, Mapping)
    ]
    after_item_ids = [
        [item.get("id") for item in turn.get("items", []) if isinstance(item, Mapping)]
        for turn in after_turns
        if isinstance(turn, Mapping)
    ]
    before_types = [
        [item.get("type") for item in turn.get("items", []) if isinstance(item, Mapping)]
        for turn in before_turns
        if isinstance(turn, Mapping)
    ]
    after_types = [
        [item.get("type") for item in turn.get("items", []) if isinstance(item, Mapping)]
        for turn in after_turns
        if isinstance(turn, Mapping)
    ]
    return {
        "thread_id_preserved": before.get("id") == after.get("id"),
        "turn_ids_preserved": [
            turn.get("id") for turn in before_turns if isinstance(turn, Mapping)
        ]
        == [turn.get("id") for turn in after_turns if isinstance(turn, Mapping)],
        "item_ids_preserved": before_item_ids == after_item_ids,
        "item_order_preserved": before_types == after_types,
        "structural_readback_preserved": _thread_structure(before)
        == _thread_structure(after),
    }


def _read_thread(client: JsonRpcClient, thread_id: str) -> dict[str, Any]:
    result = response_result(
        client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}, 30
        ),
        "issue392_thread_read",
    )
    thread = result.get("thread")
    _require(isinstance(thread, dict), "desktop_thread_read_invalid")
    return thread


def _capture_desktop_app_lifecycle(executable: Path) -> dict[str, Any]:
    client_name = "codex_desktop"
    scenario_name = "desktop_app_collaboration_v2_lifecycle"
    with IsolatedHome(scenario_name) as isolated:
        _require(isolated.path is not None, "isolated_home_missing")
        home = isolated.path
        workspace = home / "workspace"
        workspace.mkdir()
        with FixtureServer(lifecycle=True) as server:
            _write_config(home, server.server_port, v2=True, app_server=True)
            environment = _isolated_environment(home)
            app = start_issue106_app_server(executable, home, environment)
            client = JsonRpcClient(app)
            try:
                initialize(client, 30)
                thread_id = start_issue106_custom_thread(
                    client, workspace, MODEL, 30, "issue392_thread_start"
                )
                turn_id = turn_started(
                    client,
                    thread_id,
                    "Spawn worker, wait for completion, then finish.",
                    60,
                    model=MODEL,
                )
                wait_for_turn(client, thread_id, turn_id, 180)
                before = _read_thread(client, thread_id)
                notifications = list(client._notifications)
            finally:
                client.close()
                stop_issue106_app_server(app)

            app_restart = start_issue106_app_server(executable, home, environment)
            restart_client = JsonRpcClient(app_restart)
            try:
                initialize(restart_client, 30)
                resumed = response_result(
                    restart_client.request(
                        "thread/resume", {"threadId": thread_id}, 30
                    ),
                    "issue392_thread_resume",
                )
                resumed_thread = resumed.get("thread")
                _require(
                    isinstance(resumed_thread, Mapping),
                    "desktop_thread_resume_invalid",
                )
                after = _read_thread(restart_client, thread_id)
            finally:
                restart_client.close()
                stop_issue106_app_server(app_restart)
            requests = copy.deepcopy(server.requests)

        _require(isinstance(server.root_thread, str), "root_thread_missing")
        phases: dict[str, Mapping[str, Any]] = {}
        root_count = 0
        child_count = 0
        for body in requests:
            thread = (body.get("client_metadata") or {}).get("thread_id")
            if thread == server.root_thread:
                root_count += 1
                phase = (
                    "root_initial"
                    if root_count == 1
                    else "root_after_spawn"
                    if root_count == 2
                    else "root_after_wait"
                )
            else:
                child_count += 1
                phase = "child_initial" if child_count == 1 else f"child_{child_count}"
            phases[phase] = body
        _require(
            set(phases)
            == {"root_initial", "root_after_spawn", "child_initial", "root_after_wait"},
            "desktop_app_lifecycle_phase_set_invalid",
        )
        relevant_notifications = [
            summary
            for notification in notifications
            if (summary := _notification_summary(notification)) is not None
        ]
        result = _base_scenario(
            client=client_name,
            home=isolated,
            request_count=len(requests),
            process_runs=2,
        )
        result["observed"] = {
            "declaration": _declaration(
                phases["root_initial"], contract.V2_NAMESPACE
            ),
            "requests_by_phase": {
                phase: _safe_request_shape(body) for phase, body in sorted(phases.items())
            },
            "notifications": relevant_notifications,
            "thread_read_before_restart": _thread_structure(before),
            "thread_read_after_restart": _thread_structure(after),
            "resume_result_fields": sorted(resumed),
            "resume_result_field_types": _field_types(resumed),
            "identity_relationships": _desktop_identity_relationships(before, after),
        }
        return result


def _capture_binding(payload: Mapping[str, Any]) -> str:
    clone = copy.deepcopy(dict(payload))
    clone.pop("capture_run_binding_sha256", None)
    encoded = json.dumps(
        clone, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture(
    *,
    cli_executable: Path,
    desktop_executable: Path,
    cli_source_root: Path,
    desktop_source_root: Path,
) -> dict[str, Any]:
    runtimes = {
        "codex_cli": _verify_runtime(
            client="codex_cli",
            executable=cli_executable,
            source_root=cli_source_root,
            expected_binary_sha256="ae9d865f3d346a1a2a60c4e84775622d74e3e7ef53e0dede9c68b81eab306cca",
            expected_runtime_version="0.146.1",
            expected_source_commit="79b4f03d35962b005b007a015113b38930711665",
            expected_source_blobs=contract.CLI_SOURCE_BLOBS,
            client_version="0.146.1",
            source_tag="rust-v0.146.1",
        ),
        "codex_desktop": _verify_runtime(
            client="codex_desktop",
            executable=desktop_executable,
            source_root=desktop_source_root,
            expected_binary_sha256="fb5c760e14cf8fe86e12e49e8a3e7f237af06082d6b9fe1e411e463b7229c916",
            expected_runtime_version="0.147.0-alpha.6.5",
            expected_source_commit="618b8e9111da9f57fe380b09d0f6516e3f343536",
            expected_source_blobs=contract.DESKTOP_SOURCE_BLOBS,
            client_version="26.803.5235.0",
            source_tag="rust-v0.147.0-alpha.6.5",
        ),
    }
    scenarios = {
        "cli_v1_request": _capture_request_scenario(
            client="codex_cli", executable=cli_executable, version=contract.V1
        ),
        "desktop_v1_request": _capture_request_scenario(
            client="codex_desktop", executable=desktop_executable, version=contract.V1
        ),
        "cli_v2_lifecycle": _capture_v2_lifecycle(
            client="codex_cli", executable=cli_executable
        ),
        "desktop_v2_lifecycle": _capture_v2_lifecycle(
            client="codex_desktop", executable=desktop_executable
        ),
        "desktop_app_v2_lifecycle": _capture_desktop_app_lifecycle(
            desktop_executable
        ),
    }
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "candidate_revision": CANDIDATE_REVISION,
        "captured_on": date.today().isoformat(),
        "controls": {
            "explicit_runtime_capture": True,
            "fresh_empty_home_per_scenario": True,
            "workspace_under_isolated_home": True,
            "protocol_upstream_loopback": True,
            "plugin_services_disabled": list(DISABLED_FEATURES),
            "sensitive_environment_credentials_removed": True,
            "existing_user_home_read": False,
            "existing_user_task_read": False,
            "known_crash_task_read": False,
            "raw_content_or_credentials_retained": False,
            "raw_paths_or_opaque_identifiers_retained": False,
        },
        "runtimes": runtimes,
        "scenarios": scenarios,
    }
    payload["capture_run_binding_sha256"] = _capture_binding(payload)
    contract.validate_runtime_observations(payload)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-runtime-capture", action="store_true")
    parser.add_argument("--cli-exe", type=Path, required=True)
    parser.add_argument("--desktop-exe", type=Path, required=True)
    parser.add_argument("--cli-source-root", type=Path, required=True)
    parser.add_argument("--desktop-source-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.enable_runtime_capture:
        print("CAPTURE_DISABLED:enable_runtime_capture_required")
        return 2
    try:
        payload = capture(
            cli_executable=args.cli_exe.resolve(),
            desktop_executable=args.desktop_exe.resolve(),
            cli_source_root=args.cli_source_root.resolve(),
            desktop_source_root=args.desktop_source_root.resolve(),
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "capture_run_binding_sha256": payload[
                        "capture_run_binding_sha256"
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    except CaptureFailure as error:
        print(f"CAPTURE_FAILED:{error}")
        return 2
    except contract.ContractValidationError as error:
        print(f"CAPTURE_INVALID:{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
