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
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))
DEFAULT_PARENT = "xai/grok-4.6"
DEFAULT_REASONING = {
    "xai/grok-4.6": "high",
    "opencode-go/muse-spark-1.3-contributor": "xhigh",
    "commandcode/deepseek/deepseek-v4-flash": "max",
    "commandcode/z-ai/glm-5.3-flash": "max",
    "commandcode/meta/muse-spark-1.3-contributor": "max",
    "opencode-go/qwen3.8-flash": "xhigh",
    "opencode-go/hy4-preview": "high",
    "opencode-go/omen-alpha": "high",
}
SENTINELS = ("E2E_CHILD_OK 323", "E2E_FOLLOWUP_OK 667")
PARENT_SENTINEL = "E2E_PARENT_IMPLEMENTED_OK 941"
NO_SUBAGENT_TURN_SENTINEL = "E2E_NO_SUBAGENT_TURN_OK 818"


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
                try:
                    payload = json.loads(body)
                    namespaces = sorted(tool["name"] for tool in payload.get("tools", [])
                                        if isinstance(tool, dict) and tool.get("type") == "namespace")
                    from collaboration_runtime_contract import classify_collaboration_tools
                    version = classify_collaboration_tools(payload.get("tools", [])) if any(
                        name in {"collaboration", "multi_agent_v1"} for name in namespaces
                    ) else None
                    observation = {"model": payload.get("model"), "protocol": version,
                                   "namespaces": namespaces, "body_sha256": hashlib.sha256(body).hexdigest()}
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
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"connection", "content-length", "transfer-encoding"}:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
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
    return sum(
        _successful_test_item(item, test_filename)
        for record in records for item in record.get("cli_items", [])
    )


def _successful_test_item(item: dict, test_filename: str) -> bool:
    if item.get("type") != "command_execution" or item.get("status") != "completed" or item.get("exit_code") != 0:
        return False
    try:
        argv = shlex.split(item.get("command", ""))
        if len(argv) == 3 and Path(argv[0]).name in {"bash", "sh", "zsh"} and argv[1] in {"-lc", "-c"}:
            argv = shlex.split(argv[2])
    except ValueError:
        return False
    return len(argv) == 5 and argv[0] in {
        sys.executable, os.environ.get("CODEXHUB_E2E_PYTHON"), "$CODEXHUB_E2E_PYTHON",
    } and argv[1:] == ["-m", "unittest", "-q", test_filename]


def _read_client_turn(path: Path) -> dict:
    """CLI events are execution evidence; rollout prompt text is not."""
    result = {"thread_id": None, "terminal": None, "items": []}
    started, completed = set(), set()
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
        elif kind in {"turn.completed", "turn.failed"}:
            if result["terminal"] is not None:
                raise RuntimeError("duplicate_client_terminal")
            result["terminal"] = kind
        elif kind in {"item.started", "item.completed"}:
            item = event.get("item", {})
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise RuntimeError("missing_client_item_id")
            if kind == "item.started":
                if item_id in started or item_id in completed:
                    raise RuntimeError("duplicate_client_item")
                started.add(item_id)
            else:
                if item_id in completed:
                    raise RuntimeError("duplicate_client_item")
                if item.get("type") == "command_execution" and item_id not in started:
                    raise RuntimeError("missing_command_start")
                completed.add(item_id)
                result["items"].append(item)
    if started - completed:
        result["terminal"] = "incomplete_items"
    return result


def _turns(record: dict) -> list[dict]:
    turns = []
    for row in record["rows"]:
        item = row.get("payload", {})
        if row.get("type") == "event_msg" and item.get("type") == "task_started":
            turns.append({"id": item.get("turn_id"), "rows": [], "terminal": None})
        if turns:
            turns[-1]["rows"].append(row)
            if row.get("type") == "event_msg" and item.get("type") in {"task_complete", "turn_aborted"}:
                if turns[-1]["terminal"] is not None or item.get("turn_id") != turns[-1]["id"]:
                    raise RuntimeError("ambiguous_rollout_terminal")
                turns[-1]["terminal"] = item["type"]
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
        call["failed"] = isinstance(output, dict) and bool(output.get("error") or output.get("isError"))
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


def _lifecycle_passed(parent: dict, child: dict, version: str) -> tuple[bool, list[dict]]:
    calls = _paired_collaboration_calls(parent, version)
    successful = [call for call in calls if not call["failed"]]
    spawns = [call for call in successful if call["name"] == "spawn_agent"]
    if len(spawns) != 1 or not isinstance(spawns[0]["output"], dict):
        return False, calls
    spawn = spawns[0]
    output = spawn["output"]
    identity = output.get("task_name") if version == "v2" else output.get("agent_id")
    if not isinstance(identity, str) or not identity or identity != (child.get("agent_path") if version == "v2" else child["id"]):
        return False, calls
    targets = {identity}
    if version == "v2" and isinstance(spawn["arguments"].get("task_name"), str):
        targets.add(spawn["arguments"]["task_name"])
    steps = ["spawn_agent", "followup_task"] if version == "v2" else [
        "spawn_agent", "wait_agent", "close_agent", "resume_agent", "send_input", "wait_agent", "close_agent",
    ]
    position = 0
    matched_calls = []
    for call in successful:
        if position == len(steps) or call["name"] != steps[position]:
            continue
        args = call["arguments"]
        if position and version == "v2" and args.get("target") not in targets:
            continue
        if position and version == "v1":
            ids = args.get("ids") if call["name"] == "wait_agent" else [args.get("id")]
            if ids != [identity]:
                continue
            if call["name"] == "wait_agent":
                status = call["output"].get("status", {}) if isinstance(call["output"], dict) else {}
                if not isinstance(status.get(identity), dict) or "completed" not in status[identity]:
                    continue
        matched_calls.append(call)
        position += 1
    child_turns = _turns(child)
    lifecycle = position == len(steps) and len(child_turns) == 2 and all(
        turn["terminal"] == "task_complete" for turn in child_turns
    )
    completions = [row for row in child["rows"] if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "task_complete"]
    if not lifecycle or len(completions) != 2:
        return False, calls
    messages = [row["payload"].get("last_agent_message") for row in completions]
    if not all(isinstance(message, str) and message.strip() for message in messages):
        return False, calls
    followup = next(call for call in matched_calls if call["name"] == ("followup_task" if version == "v2" else "send_input"))
    first_done, second_done = [row.get("timestamp") for row in completions]
    if not all(isinstance(value, str) for value in (first_done, second_done, followup.get("timestamp"))) or not (
        first_done <= followup["timestamp"] <= second_done
    ):
        return False, calls
    if version == "v2":
        delivered = [row.get("payload", {}) for row in parent["rows"]
                     if row.get("type") == "response_item" and row.get("payload", {}).get("type") == "agent_message"
                     and row["payload"].get("author") == identity
                     and row["payload"].get("recipient") == identity.rsplit("/", 1)[0]]
        if not all(any(message in _item_text(item.get("content")) for item in delivered) for message in messages):
            return False, calls
    # Correlate the parent edit request/result with the CLI file-change event.
    # Without the child's completed review before that edit, completion text
    # alone would wrongly accept a parent that bypassed the review altogether.
    last_review = completions[-1].get("timestamp")
    if any(row.get("timestamp", "") < last_review for row in parent["rows"]
           if row.get("type") == "response_item" and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
           and row["payload"].get("name") == "apply_patch"):
        return False, calls
    patch_ids = {row["payload"].get("call_id") for row in parent["rows"]
                 if row.get("type") == "response_item" and row.get("payload", {}).get("type") in {"custom_tool_call", "function_call"}
                 and row["payload"].get("name") == "apply_patch"
                 and isinstance(last_review, str) and row.get("timestamp", "") >= last_review}
    patch_results = {row["payload"].get("call_id") for row in parent["rows"]
                     if row.get("type") == "response_item" and row.get("payload", {}).get("type") in {"custom_tool_call_output", "function_call_output"}}
    return bool((patch_ids - {None}) & patch_results), calls


def _parent_session_id(home: Path, parent_model: str) -> str | None:
    """Return the one real parent associated with a spawned child, if known.

    ``--last`` is unsafe here because a child may be the most recent session.
    The parent relationship recorded by the client is the only authority for
    selecting the session to resume.
    """
    records = _read_session_records(home)
    child_parent_ids = {
        record["parent_id"]
        for record in records
        if isinstance(record.get("parent_id"), str)
    }
    candidates = [
        record["id"]
        for record in records
        if record.get("id") in child_parent_ids and record.get("model") == parent_model
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
    for turn in client_turns:
        parent = records_by_id.get(turn["thread_id"])
        if parent is None or parent.get("parent_id") is not None:
            raise RuntimeError("client_parent_relationship_mismatch")
        if parent not in parent_records:
            parent_records.append(parent)
        parent.setdefault("cli_items", []).extend(turn["items"])
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
    expected_calls = (
        {"spawn_agent", "followup_task", "wait_agent"}
        if collaboration_version == "v2"
        else {"spawn_agent", "resume_agent", "wait_agent", "close_agent"}
    )
    handoff_contract_passed = (
        plaintext_handoffs >= 2 and encrypted_handoffs == 0
        if collaboration_version == "v2"
        # V1 has no V2 agent_message lifecycle. Its child/parent association
        # is demonstrated by the client thread relationship and paired native
        # calls, so an absent V2 handoff is expected rather than a failure.
        else plaintext_handoffs == 0 and encrypted_handoffs == 0
    )
    lifecycle_passed, calls = (False, [])
    if len(parent_records) == 1:
        calls = _paired_collaboration_calls(parent_records[0], collaboration_version)
        if len(child_records) == 1:
            lifecycle_passed, calls = _lifecycle_passed(parent_records[0], child_records[0], collaboration_version)
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
    tests_and_terminals_verified = bool(client_turns) and len(client_turns) == len(parent_turns) and all(
        turn["terminal"] == "turn.completed" and parent_turns[index]["terminal"] == "task_complete"
        and any(_successful_test_item(item, "test_parent_task.py" if index == 0 else "test_resume_turn.py") for item in turn["items"])
        for index, turn in enumerate(client_turns)
    )
    parent_edits_verified = bool(client_turns) and all(
        any(item.get("type") == "file_change" and item.get("status") == "completed"
                and any(Path(change.get("path", "")).name == "parent_task.py" and change.get("kind") == "update"
                        for change in item.get("changes", [])) for item in turn["items"])
        for turn in client_turns
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
    if tests_and_terminals_verified and not parent_edits_verified:
        # A shell edit need not emit file_change. Absence of attribution is
        # neither proof of model failure nor permission to accept the run.
        missing_evidence.append("parent_source_edit_attribution")
    attribution_only_gap = bool(missing_evidence) and lifecycle_passed and contexts_match and no_subagent_turn and child_inspection_verified
    return {
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
        "parent_child_relationships": [
            {"child": child, "parent": parent} for child, parent in relationships
        ],
        "parent_test_tool_call_count": _parent_test_tool_call_count(parent_records),
        "parent_resume_test_tool_call_count": _parent_resume_test_tool_call_count(
            parent_records
        ),
        "passed": passed,
        "status": "passed" if passed else "unverified" if attribution_only_gap or not client_outputs else "failed",
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
        "normalize(7) returns exactly `FIXED`. Use apply_patch for the source edit so the client "
        "records its attribution. Then run `\"$CODEXHUB_E2E_PYTHON\" -m unittest -q test_parent_task.py`. "
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
        "import os, sys, unittest\nfrom pathlib import Path\n"
        "if sys.version_info < (3, 13) or not os.environ.get('CODEXHUB_E2E_PYTHON') "
        "or Path(sys.executable).resolve() != Path(os.environ['CODEXHUB_E2E_PYTHON']).resolve():\n"
        "    raise RuntimeError('fixture_python_binding_mismatch')\n"
        "from parent_task import normalize\n\n"
        "class ParentTaskTest(unittest.TestCase):\n"
        "    def test_normalize(self):\n"
        "        self.assertEqual(normalize(7), 'FIXED')\n\n"
        + ("    def test_resume(self):\n        self.assertEqual(normalize(8), 'RESUMED')\n\n" if resume else "")
        + "if __name__ == '__main__':\n    unittest.main()\n"
    )


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
    expected_test = _fixture_test(test_name == "test_resume_turn.py")
    test = work / test_name
    if not source.is_file() or not test.is_file() or test.read_text(encoding="utf-8") != expected_test:
        return {fixed_key: False, exit_code_key: 1}
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
                "validator_rejection": "unsupported_fixture_source"}
    try:
        # Execute a copied source against the harness-owned test in a separate
        # directory. Neither mutable fixture tests nor source spelling decide PASS.
        with tempfile.TemporaryDirectory(prefix="codexhub-trusted-validator-") as directory:
            trusted = Path(directory)
            (trusted / source_name).write_text(source_text, encoding="utf-8")
            (trusted / test_name).write_text(expected_test, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-E", "-s", "-m", "unittest", "-q", test_name],
                cwd=trusted, capture_output=True, text=True, timeout=20,
            )
        test_exit_code: int | None = result.returncode
    except (OSError, subprocess.TimeoutExpired):
        test_exit_code = None
    return {fixed_key: test_exit_code == 0, exit_code_key: test_exit_code}


def _no_subagent_turn_prompt() -> str:
    return (
        "This is a new user turn in the same parent task. Do not inspect, call, "
        "resume, create, or delegate to any subagent or collaboration tool. Work "
        "yourself: edit parent_task.py so normalize(7) remains exactly `FIXED` and "
        "normalize(8) returns exactly `RESUMED`. Use apply_patch for the source edit. "
        "Then run `\"$CODEXHUB_E2E_PYTHON\" -m unittest -q "
        "test_resume_turn.py`. Report "
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


def _run_client(
    command: list[str],
    *,
    environment: dict[str, str],
    output: Path,
    working_directory: Path,
    timeout: int,
) -> int | None:
    """Run one explicitly requested client turn in the isolated fixture root."""
    with output.open("a", encoding="utf-8") as stream:
        client = subprocess.Popen(
            command,
            env=environment,
            cwd=working_directory,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return client.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_client_process(client)
            return None


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--parent-model", default=DEFAULT_PARENT)
    parser.add_argument("--parent-effort")
    parser.add_argument("--parent-collaboration-version", choices=("v1", "v2"), default="v2")
    parser.add_argument("--refresh-official", action="store_true")
    parser.add_argument("--child-model")
    parser.add_argument("--child-effort")
    parser.add_argument("--gateway-root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path, default=Path("test-results/third-party-collaboration.json"))
    args = parser.parse_args()
    gateway_root = args.gateway_root.resolve()
    cli_version = subprocess.check_output(["codex", "--version"], text=True).strip()
    if cli_version != "codex-cli 0.153.4":
        parser.error("requires codex-cli 0.153.4")
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
        "report_version": 4,
        "cli_version": cli_version,
        "gateway_sha": subprocess.check_output(["git", "-C", str(gateway_root), "rev-parse", "HEAD"], text=True).strip(),
        "gateway_dirty": bool(subprocess.check_output(["git", "-C", str(gateway_root), "status", "--porcelain"], text=True).strip()),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parent_model": args.parent_model,
        "parent_effort": parent_effort,
        "child_model": child_model,
        "child_effort": child_effort,
        "parent_collaboration_version": args.parent_collaboration_version,
        "passed": False,
    }
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
            load_providers(source / "proxy/config/providers.toml"), require_api_key=False,
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
        reviewer_config = client_home / "reviewer.toml"
        reviewer_config.write_text(
            'sandbox_mode = "read-only"\n' + f'model = {json.dumps(child_model)}\n'
            + f'model_reasoning_effort = {json.dumps(child_effort)}\n', encoding="utf-8",
        )
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
            '[agents.reviewer]\ndescription = "Read-only fixture reviewer"\n'
            f'config_file = {json.dumps(str(reviewer_config))}\n'
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
                )
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
                parent_session_id = _parent_session_id(client_home, args.parent_model)
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
                    report["resume_client_exit_code"] = _run_client(
                        [
                            "codex",
                            "exec",
                            "resume",
                            "--json",
                            "--skip-git-repo-check",
                            parent_session_id,
                            _no_subagent_turn_prompt(),
                        ],
                        environment=client_env,
                        output=client_output,
                        working_directory=work,
                        timeout=args.timeout,
                    )
                elif first_turn_passed:
                    report["failure_type"] = "parent_session_unavailable"

                report.update(
                    collect_evidence(
                        client_home,
                        child_model,
                        args.parent_collaboration_version,
                        parent_model=args.parent_model,
                        parent_effort=parent_effort,
                        child_effort=child_effort,
                        client_outputs=client_outputs,
                    )
                )
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
                expected_protocol = "collaboration_v1" if args.parent_collaboration_version == "v1" else "collaboration_v2"
                protocols = {entry.get("protocol") for entry in observer.observations if entry.get("protocol")}
                report["request_namespace_verified"] = protocols == {expected_protocol}
                if not report["request_namespace_verified"]:
                    report["passed"] = False
                    report["status"] = "unverified"
    if report.get("status") != "unverified":
        report["status"] = "passed" if report["passed"] else "failed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
