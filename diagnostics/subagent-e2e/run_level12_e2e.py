from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT_ROOT = REPO / "diagnostics" / "subagent-e2e"
SKILL_PATH = Path(
    r"C:\Users\noirb\.codex\plugins\cache\openai-curated-remote\superpowers\5.1.4\skills\subagent-driven-development\SKILL.md"
)
SHORT_PLAN_PATH = REPO / "diagnostics" / "subagent-e2e-cli" / "short-subagent-development-plan.md"

MODELS = [
    ("glm52", "glm-5.2"),
    ("k2_7", "kimi-k2.7-code"),
    ("m3", "minimax-m3"),
]
ENDPOINTS = [
    ("responses", "ollama-e2e-responses", "responses_structured"),
    ("chat", "ollama-e2e-chat", "chat_tools"),
]
_PROGRESS_LOCK = threading.Lock()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def git_status_baseline() -> str:
    result = subprocess.run(
        ["git", "status", "--short", "--untracked-files=normal"],
        cwd=str(REPO),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    lines = []
    for line in result.stdout.splitlines():
        normalized = line.replace("\\", "/")
        if "diagnostics/subagent-e2e/" in normalized:
            continue
        lines.append(line)
    if result.returncode != 0:
        lines.append(f"<git status exited {result.returncode}>")
    return "\n".join(lines).strip() or "<empty>"


def provider_config(upstream_base_url: str) -> str:
    upstream_base_url = upstream_base_url.rstrip("/")
    models = "\n".join(
        textwrap.dedent(
            f"""
              [[providers.models]]
              id = "{model}"
              upstream_model = "{model}"
              gateway_exported = true
              enabled = true
              context_window = 524288
              max_output_tokens = 131072
            """
        ).strip()
        for _, model in MODELS
    )
    return textwrap.dedent(
        f"""
        [[providers]]
        id = "ollama-e2e-responses"
        name = "Ollama E2E Responses"
        base_url = "{upstream_base_url}"
        api_key = "{{env:OLLAMA_API_KEY}}"
        upstream_format = "responses"
        tool_protocol = "responses_structured"
        display_prefix = "Ollama E2E Responses"
        enabled = true

        {models}

        [[providers]]
        id = "ollama-e2e-chat"
        name = "Ollama E2E Chat"
        base_url = "{upstream_base_url}"
        api_key = "{{env:OLLAMA_API_KEY}}"
        upstream_format = "chat_completions"
        tool_protocol = "chat_tools"
        display_prefix = "Ollama E2E Chat"
        enabled = true

        {models}
        """
    ).strip() + "\n"


def wait_for_gateway(port: int, proc: subprocess.Popen[bytes], timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"gateway exited early with code {proc.returncode}")
        try:
            with urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as response:
                if response.status < 500:
                    return
        except (OSError, URLError) as exc:
            last_error = str(exc)
        time.sleep(0.4)
    raise RuntimeError(f"gateway did not become ready on port {port}: {last_error}")


def start_gateway(
    run_dir: Path,
    port: int,
    upstream_base_url: str,
    subagent_mode: str,
    main_retry_attempts: int,
) -> tuple[subprocess.Popen[bytes], Path]:
    codex_home = run_dir / "codex-home"
    providers_path = codex_home / "proxy" / "config" / "providers.toml"
    write_text(providers_path, provider_config(upstream_base_url))
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env["PYTHONPATH"] = str(REPO / "src-python")
    env["CODEXHUB_SUBAGENT_ASSIST_MODE"] = subagent_mode
    env["CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS"] = str(max(1, main_retry_attempts))
    stdout = (run_dir / "gateway.stdout.txt").open("wb")
    stderr = (run_dir / "gateway.stderr.txt").open("wb")
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "src-python" / "codex_proxy.py"), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        env=env,
        stdout=stdout,
        stderr=stderr,
    )
    wait_for_gateway(port, proc)
    write_text(run_dir / "gateway.pid", str(proc.pid) + "\n")
    return proc, codex_home


def stop_gateway(proc: subprocess.Popen[bytes], port: int) -> None:
    try:
        urlopen(Request(f"http://127.0.0.1:{port}/shutdown", data=b"", method="POST"), timeout=3).read()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def level1_single_prompt(sentinel: str) -> str:
    return f"""Execute one real Codex native subagent lifecycle.

You are the coordinator. You must use the visible native subagent tools; do not use shell, file reads, tool_search, or textual simulation.
Copy exact child prompt strings verbatim. Do not add, remove, or duplicate any colon, punctuation, or whitespace inside the exact prompt strings.

Required sequence:
1. Spawn exactly one child agent.
2. The child prompt must be exactly this complete string: `Return exactly this line: {sentinel}`
3. Wait for that child.
4. Close that child.
5. Final response must be exactly four lines:
SPAWNED: yes
AGENT_ID: <the exact child agent id>
SENTINEL_SEEN: yes|no
CLOSED: yes|no
"""


def level1_two_prompt(sentinel_a: str, sentinel_b: str) -> str:
    return f"""Execute a bounded concurrent two-agent Codex native subagent lifecycle.

You are the coordinator. You must use the visible native subagent tools; do not use shell, file reads, tool_search, or textual simulation.
Copy exact child prompt strings verbatim. Do not add, remove, or duplicate any colon, punctuation, or whitespace inside the exact prompt strings.

Required sequence:
1. Spawn child A with prompt exactly this complete string: `Return exactly this line: {sentinel_a}`
2. Spawn child B with prompt exactly this complete string: `Return exactly this line: {sentinel_b}`
3. Do not wait before both children have been spawned.
4. Wait for both exact child agent ids.
5. Close both exact child agent ids.
6. Do not spawn any extra child.
7. Final response must be exactly six lines:
SPAWN_COUNT: 2
AGENT_IDS: <id-a>,<id-b>
SENTINEL_A_SEEN: yes|no
SENTINEL_B_SEEN: yes|no
CLOSED_COUNT: 2
EXTRA_SPAWN: no
"""


def level2_prompt(
    case_name: str,
    model: str,
    endpoint: str,
    output_path: Path,
    sentinel: str,
    baseline_status: str,
) -> str:
    return f"""[$superpowers:subagent-driven-development]({SKILL_PATH})

Use the real subagent-driven-development skill and this short diagnostic plan:
{SHORT_PLAN_PATH}

Coordinator inputs:
OUTPUT_PATH={output_path}
SENTINEL={sentinel}
MODEL_UNDER_TEST={model}
ENDPOINT_UNDER_TEST={endpoint}
CASE={case_name}

Baseline git status before this E2E case started. These entries are pre-existing and must not be blamed on this diagnostic run:
```text
{baseline_status}
```

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. The coordinator may read the plan once, but must not create, edit, inspect, or verify OUTPUT_PATH directly. All artifact work and verification must be delegated to subagents.
3. The implementer creates exactly one diagnostic artifact at OUTPUT_PATH.
4. The spec reviewer verifies exact file content and required sentinel.
5. The code-quality reviewer verifies minimal implementation and no product-source modifications introduced after the baseline above. Existing product-source modifications already listed in the baseline are allowed and must not cause failure; the expected OUTPUT_PATH under diagnostics is also allowed.
   When spawning the code-quality reviewer, explicitly instruct it to ignore runner-owned diagnostics scaffolding in the current E2E run directory, including prompt/run/stdout/stderr/last/summary/progress/gateway/codex-home files and repeat artifacts for other rXX cases. Those files are created by the harness, not by the implementer. Minimalism fails only if the implementer created files other than OUTPUT_PATH or if git status shows new product-source modifications outside the baseline.
6. Start with this ordered lifecycle: spawn one implementer, wait, close; then spawn one spec reviewer, wait, close; then spawn one code-quality reviewer, wait, close.
7. If a reviewer finds issues, route a focused fix back through an implementer subagent, then re-run the same reviewer stage before proceeding. Do not spawn independent verifiers or extra reviewers unless the previous reviewer reported FAIL/BLOCKED or returned no usable result.
8. Do not call local_tool_gateway or any mcp__codex_apps__local_tool_gateway* tool in the coordinator or subagent prompts. If the coordinator needs to read the plan, use node_repl or another visible native file-read-capable tool. Subagents should use shell/apply_patch-capable native tools, not local_tool_gateway.
9. Final coordinator response must be exactly:
RESULT: PASS|FAIL
{sentinel}
SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer
"""


def level3_dynamic_dag_prompt(case_name: str) -> str:
    return f"""Run LEVEL3_DYNAMIC_DAG using real Codex native subagents.

Case: {case_name}

Workflow:
1. Spawn node task-a-implementer first. Set nickname exactly to task-a-implementer.
2. Wait for task-a-implementer and close it after it returns A_DONE.
3. After task-a-implementer is closed, spawn both ready nodes:
   - task-a-reviewer, nickname task-a-reviewer, returns A_REVIEW_PASS
   - task-b-implementer, nickname task-b-implementer, returns B_DONE
   These two nodes may be spawned in either order or in the same turn.
4. Wait for both branch nodes and close both after their expected outputs are returned.
5. Spawn final-summarizer, nickname final-summarizer, after both branch nodes are closed.
6. Wait for final-summarizer and close it after it returns FINAL_READY.

Worker prompt rule:
Every worker prompt must contain a line `Node: <node_id>`.
Workers must not call multi_agent tools and must not create or modify files.

Coordinator constraints:
Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.
Do not directly perform worker outputs yourself.
Do not write final response until final-summarizer is closed.

Final coordinator response must be exactly:
RESULT: PASS
DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer
DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done
"""


def run_codex_case(
    *,
    run_dir: Path,
    case_name: str,
    prompt: str,
    model_id: str,
    port: int,
    timeout: int,
    ephemeral_cli: bool,
    preserve_cli_tools: bool,
    subagent_mode: str,
    main_retry_attempts: int,
) -> dict[str, Any]:
    prompt_path = run_dir / f"{case_name}.prompt.txt"
    stdout_path = run_dir / f"{case_name}.stdout.jsonl"
    stderr_path = run_dir / f"{case_name}.stderr.txt"
    last_path = run_dir / f"{case_name}.last.txt"
    write_text(prompt_path, prompt)

    codex_bin = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    if not codex_bin:
        raise RuntimeError("codex executable not found on PATH")
    command = [
        codex_bin,
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(REPO),
        "-m",
        model_id,
        "-c",
        'model_provider="custom"',
        "-c",
        'model_providers.custom.name="CodexHub E2E Gateway"',
        "-c",
        f'model_providers.custom.base_url="http://127.0.0.1:{port}/v1"',
        "-c",
        'model_providers.custom.wire_api="responses"',
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        "model_providers.custom.supports_websockets=false",
        "-o",
        str(last_path),
        "-",
    ]
    if not preserve_cli_tools:
        command[command.index("-o"):command.index("-o")] = [
            "-c",
            "agents={}",
            "-c",
            "plugins={}",
        ]
    if ephemeral_cli:
        command.insert(2, "--ephemeral")
    env = os.environ.copy()
    env["CODEXHUB_SUBAGENT_ASSIST_MODE"] = subagent_mode
    env["CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS"] = str(max(1, main_retry_attempts))
    result = {
        "case": case_name,
        "model": model_id,
        "exit_code": None,
        "timed_out": False,
        "duration_seconds": None,
        "prompt": str(prompt_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "last": str(last_path),
        "run_dir": str(run_dir),
        "ephemeral_cli": ephemeral_cli,
        "preserve_cli_tools": preserve_cli_tools,
        "subagent_mode": subagent_mode,
        "main_retry_attempts": max(1, main_retry_attempts),
    }
    run_json_path = run_dir / f"{case_name}.run.json"
    write_text(run_json_path, json.dumps(result, indent=2, ensure_ascii=True) + "\n")

    started = time.time()
    try:
        with prompt_path.open("rb") as stdin, stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            popen_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                command,
                cwd=str(REPO),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env=env,
                **popen_kwargs,
            )
            deadline = started + timeout
            while True:
                exit_code = proc.poll()
                if exit_code is not None:
                    result["exit_code"] = exit_code
                    break
                if time.time() >= deadline:
                    result["timed_out"] = True
                    terminate_process_tree(proc)
                    try:
                        result["exit_code"] = proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        terminate_process_tree(proc)
                        result["exit_code"] = proc.returncode
                    break
                time.sleep(1)
    except BaseException as exc:
        result["exception"] = repr(exc)
        raise
    finally:
        result["duration_seconds"] = round(time.time() - started, 3)
        write_text(run_json_path, json.dumps(result, indent=2, ensure_ascii=True) + "\n")
    return result


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    parts: list[str] = []
    for key in ("message", "aggregated_output", "text"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
    content = item.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "\n".join(parts)


def agent_state_messages(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {}
    states = item.get("agents_states")
    if not isinstance(states, dict):
        return {}
    messages: dict[str, str] = {}
    for agent_id, state in states.items():
        if not isinstance(agent_id, str) or not isinstance(state, dict):
            continue
        message = state.get("message")
        if isinstance(message, str) and message:
            messages[agent_id] = message
    return messages


def parse_cli_events(stdout_path: Path) -> dict[str, Any]:
    raw_events = load_jsonl(stdout_path)
    collab: list[dict[str, Any]] = []
    final_texts: list[str] = []
    errors: list[str] = []
    thread_id: str | None = None
    for event in raw_events:
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        item = event.get("item")
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            errors.append(event["message"])
        if not isinstance(item, dict):
            continue
        if item.get("type") == "error" and isinstance(item.get("message"), str):
            errors.append(item["message"])
        if item.get("type") == "collab_tool_call":
            collab.append(
                {
                    "event": event.get("type"),
                    "id": item.get("id"),
                    "tool": item.get("tool"),
                    "receivers": item.get("receiver_thread_ids") or [],
                    "status": item.get("status"),
                    "prompt": item.get("prompt"),
                    "messages": item.get("messages") or {},
                    "agent_messages": agent_state_messages(item),
                }
            )
        elif event.get("type") == "item.completed" and item.get("type") != "error":
            text = item_text(item)
            if text:
                final_texts.append(text)
    return {
        "events": raw_events,
        "collab": collab,
        "final_text": (final_texts[-1] if final_texts else ""),
        "errors": errors,
        "thread_id": thread_id,
    }


def completed_tool_calls(parsed: dict[str, Any], tool_names: set[str]) -> list[dict[str, Any]]:
    return [
        event
        for event in parsed["collab"]
        if event.get("event") == "item.completed"
        and event.get("status") == "completed"
        and event.get("tool") in tool_names
    ]


def completed_collab_events(parsed: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, event)
        for index, event in enumerate(parsed["collab"])
        if event.get("event") == "item.completed" and event.get("status") == "completed"
    ]


def event_agent_ids(event: dict[str, Any]) -> set[str]:
    ids = {receiver for receiver in (event.get("receivers") or []) if isinstance(receiver, str)}
    for key in ("messages", "agent_messages"):
        value = event.get(key)
        if isinstance(value, dict):
            ids.update(str(agent_id) for agent_id in value if isinstance(agent_id, str) and agent_id)
    return ids


def lifecycle_completed_between(
    ordered_events: list[tuple[int, dict[str, Any]]],
    spawn_event: dict[str, Any],
    start_index: int,
    end_index: int | None,
) -> bool:
    agent_ids = event_agent_ids(spawn_event)
    if not agent_ids:
        return False

    wait_positions: list[int] = []
    close_positions: list[int] = []
    for index, event in ordered_events:
        if index <= start_index:
            continue
        if end_index is not None and index >= end_index:
            continue
        event_ids = event_agent_ids(event)
        if not agent_ids.intersection(event_ids):
            continue
        tool = event.get("tool")
        if tool in {"wait", "wait_agent"}:
            wait_positions.append(index)
        elif tool == "close_agent":
            close_positions.append(index)
    return any(wait_index < close_index for wait_index in wait_positions for close_index in close_positions)


def all_message_text(parsed: dict[str, Any]) -> str:
    parts = [parsed.get("final_text") or ""]
    for event in parsed["collab"]:
        messages = event.get("messages")
        if isinstance(messages, dict):
            parts.extend(str(value) for value in messages.values())
        agent_messages = event.get("agent_messages")
        if isinstance(agent_messages, dict):
            parts.extend(str(value) for value in agent_messages.values())
    return "\n".join(parts)


def router_errors(parsed: dict[str, Any], stderr_path: Path) -> list[str]:
    text = "\n".join(parsed.get("errors") or [])
    if stderr_path.exists():
        text += "\n" + stderr_path.read_text(encoding="utf-8", errors="replace")
    errors = []
    for line in text.splitlines():
        lowered = line.lower()
        if "tools::router: error=exit code:" in lowered:
            continue
        if "failed to parse function arguments" in lowered and (
            "expected `explanation` or `plan`" in lowered
            or (
                "expected a sequence" in lowered
                and ('"step"' in lowered or '\\"step\\"' in lowered)
                and ('"status"' in lowered or '\\"status\\"' in lowered)
            )
        ):
            continue
        is_subagent_related = any(
            token in lowered
            for token in (
                "multi_agent",
                "spawn_agent",
                "wait_agent",
                "close_agent",
                "resume_agent",
                "send_input",
                "subagent",
            )
        )
        is_native_router = "native router" in lowered or "router error" in lowered
        is_tool_router = "tools::router: error" in lowered or "router: error=" in lowered
        is_tool_shape = any(
            needle in lowered
            for needle in (
                "failed to parse function arguments",
                "unsupported tool",
                "invalid tool",
                "unknown tool",
                "unsupported call",
            )
        )
        if is_subagent_related and (is_native_router or is_tool_router or is_tool_shape):
            errors.append(line)
    return errors


def proxy_event_counts_for_case(case: dict[str, Any], parsed: dict[str, Any]) -> dict[str, int]:
    thread_id = parsed.get("thread_id")
    run_dir_value = case.get("run_dir")
    run_dir = Path(run_dir_value) if isinstance(run_dir_value, str) and run_dir_value else Path(case["stdout"]).parent
    events_path = run_dir / "codex-home" / "proxy" / "codex-proxy-events.jsonl"
    counts = {
        "required_subagent_call_repaired": 0,
        "upstream_retry": 0,
        "sse_retry_notice": 0,
        "chat_to_responses_event_summary": 0,
        "upstream_stream_error": 0,
        "upstream_stream_incomplete": 0,
        "upstream_stream_interrupted": 0,
        "upstream_stream_idle_timeout": 0,
        "cli_stream_reconnect": 0,
        "lifecycle_empty_final_response": 0,
        "lifecycle_empty_final_resample": 0,
        "native_router_error": 0,
    }
    for event in parsed.get("events") or []:
        if not isinstance(event, dict) or event.get("type") != "error":
            continue
        message = event.get("message")
        if isinstance(message, str) and "stream disconnected" in message.lower():
            counts["cli_stream_reconnect"] += 1
    if not isinstance(thread_id, str) or not thread_id or not events_path.exists():
        return counts
    prefix = f"{thread_id}:"
    for event in load_jsonl(events_path):
        window_id = event.get("window_id")
        if isinstance(window_id, str) and not window_id.startswith(prefix):
            continue
        name = event.get("event")
        if name in counts:
            counts[name] += 1
        if (
            name == "chat_stream_shape_summary"
            and event.get("subagent_lifecycle_complete") is True
            and int(event.get("text_chars") or 0) == 0
            and int(event.get("tool_call_count") or 0) == 0
        ):
            counts["lifecycle_empty_final_response"] += 1
        text = json.dumps(event, ensure_ascii=False)
        lowered = text.lower()
        if "native router" in lowered or ("multi_agent_v1" in lowered and "error" in lowered):
            counts["native_router_error"] += 1
    return counts


def classify_failure(summary: dict[str, Any]) -> str:
    if summary.get("pass"):
        return "none"
    checks = summary.get("checks") if isinstance(summary.get("checks"), dict) else {}
    if not checks.get("exit_code_zero", True) or summary.get("timed_out"):
        if (
            summary.get("upstream_stream_error")
            or summary.get("upstream_stream_idle_timeout")
            or summary.get("cli_stream_reconnect")
        ):
            return "provider_stream_flake"
        return "timeout"
    if summary.get("native_router_error"):
        return "adapter_defect"
    if not checks.get("completed_spawn_count", True):
        return "model_choice"
    if not checks.get("wait_covers_agents", True) or not checks.get("close_covers_agents", True):
        return "protocol_or_policy_defect"
    if not checks.get("role_lifecycle_order", True) or not checks.get("dependency_order", True):
        return "scheduler_or_model_prompt_defect"
    if not checks.get("sentinels_seen", True):
        return "scheduler_or_model_prompt_defect"
    if not checks.get("worker_outputs_seen", True):
        return "scheduler_or_model_prompt_defect"
    if not checks.get("final_exact", True) or not checks.get("artifact_exact", True):
        return "workflow_output_defect"
    return "unclassified"


def analyze_level1(case: dict[str, Any], scenario: str, sentinels: list[str]) -> dict[str, Any]:
    stdout_path = Path(case["stdout"])
    stderr_path = Path(case["stderr"])
    parsed = parse_cli_events(stdout_path)
    spawns = completed_tool_calls(parsed, {"spawn_agent"})
    waits = completed_tool_calls(parsed, {"wait", "wait_agent"})
    closes = completed_tool_calls(parsed, {"close_agent"})
    spawn_ids: list[str] = []
    for spawn in spawns:
        for receiver in spawn.get("receivers") or []:
            if isinstance(receiver, str) and receiver not in spawn_ids:
                spawn_ids.append(receiver)
    wait_ids = {
        receiver
        for wait in parsed["collab"]
        if wait.get("tool") in {"wait", "wait_agent"}
        for receiver in (wait.get("receivers") or [])
        if isinstance(receiver, str)
    }
    close_ids = {receiver for close in closes for receiver in (close.get("receivers") or []) if isinstance(receiver, str)}
    transcript = all_message_text(parsed)
    required_count = 1 if scenario == "single" else 2
    router = router_errors(parsed, stderr_path)
    proxy_counts = proxy_event_counts_for_case(case, parsed)
    pass_checks = {
        "exit_code_zero": case.get("exit_code") == 0,
        "not_timed_out": not case.get("timed_out"),
        "completed_spawn_count": len(spawns) == required_count,
        "unique_agent_count": len(spawn_ids) == required_count,
        "wait_covers_agents": set(spawn_ids).issubset(wait_ids) and len(wait_ids) >= required_count,
        "close_covers_agents": set(spawn_ids).issubset(close_ids) and len(close_ids) >= required_count,
        "sentinels_seen": all(sentinel in transcript for sentinel in sentinels),
        "no_router_errors": not router and proxy_counts["native_router_error"] == 0,
        "no_extra_completed_spawns": len(spawns) == required_count,
    }
    if scenario == "single":
        pass_checks["final_reports_spawned"] = "SPAWNED: yes" in parsed["final_text"]
        pass_checks["final_reports_closed"] = "CLOSED: yes" in parsed["final_text"]
    else:
        pass_checks["final_reports_two"] = "SPAWN_COUNT: 2" in parsed["final_text"]
        pass_checks["final_reports_no_extra"] = "EXTRA_SPAWN: no" in parsed["final_text"]
    summary = {
        **case,
        **proxy_counts,
        "scenario": scenario,
        "pass": all(pass_checks.values()),
        "checks": pass_checks,
        "agent_ids": spawn_ids,
        "sentinels": sentinels,
        "router_errors": router,
        "tool_counts": {
            "completed_spawn": len(spawns),
            "completed_wait": len(waits),
            "completed_close": len(closes),
        },
        "final_text": parsed["final_text"],
    }
    summary["failure_classification"] = classify_failure(summary)
    summary["protocol_lock_relevant"] = scenario == "single" and summary["failure_classification"] in {
        "none",
        "protocol_or_policy_defect",
        "adapter_defect",
    }
    write_text(Path(case["stdout"]).with_suffix(".parsed.json"), json.dumps(summary, indent=2, ensure_ascii=True) + "\n")
    return summary


def role_from_prompt(prompt: Any) -> str | None:
    if not isinstance(prompt, str):
        return None
    lowered = prompt.lower()
    if (
        re.search(r"\byou are (?:(?:the|a|an) )?(?:code-quality|code quality|quality)[^\n.]*reviewer\b", lowered)
        or "code-quality reviewer subagent" in lowered
        or "code quality reviewer subagent" in lowered
    ):
        return "quality-reviewer"
    if re.search(r"\brole:\s*(?:code[-_ ]quality|quality)[ _-]?reviewer\b", lowered):
        return "quality-reviewer"
    if (
        re.search(r"\byou are (?:(?:the|a|an) )?spec[^\n.]*reviewer\b", lowered)
        or "spec reviewer subagent" in lowered
        or "spec compliance reviewer subagent" in lowered
    ):
        return "spec-reviewer"
    if re.search(r"\brole:\s*spec[ _-]?reviewer\b", lowered):
        return "spec-reviewer"
    if re.search(r"\byou are (?:(?:the|a|an) )?implementer\b", lowered) or "implementer subagent" in lowered:
        return "implementer"
    if re.search(r"\brole:\s*implementer\b", lowered):
        return "implementer"
    if re.search(r"\byou are implementing task\s+\d+\b", lowered):
        return "implementer"
    if re.search(r"\bdescription:\s*\"implement\b", lowered):
        return "implementer"
    return None


def level2_role_order_valid(roles: list[str]) -> bool:
    if not roles or roles[0] != "implementer":
        return False
    try:
        first_spec = roles.index("spec-reviewer")
        first_quality = roles.index("quality-reviewer")
    except ValueError:
        return False
    if not (0 < first_spec < first_quality):
        return False

    for previous, current in zip(roles, roles[1:]):
        if previous == "quality-reviewer" and current == "spec-reviewer":
            return False
    return True


def level2_role_lifecycle_order_valid(parsed: dict[str, Any]) -> bool:
    ordered = completed_collab_events(parsed)
    role_spawns: list[tuple[int, str, dict[str, Any]]] = []
    for index, event in ordered:
        if event.get("tool") != "spawn_agent":
            continue
        role = role_from_prompt(event.get("prompt"))
        if role:
            role_spawns.append((index, role, event))

    chain: list[tuple[int, str, dict[str, Any]]] = []
    cursor = -1
    for expected_role in ("implementer", "spec-reviewer", "quality-reviewer"):
        match = next(
            (
                (index, role, event)
                for index, role, event in role_spawns
                if index > cursor and role == expected_role
            ),
            None,
        )
        if match is None:
            return False
        chain.append(match)
        cursor = match[0]

    for current, next_item in zip(chain, chain[1:]):
        if not lifecycle_completed_between(ordered, current[2], current[0], next_item[0]):
            return False
    return lifecycle_completed_between(ordered, chain[-1][2], chain[-1][0], None)


def contains_path_reference(value: Any, path: Path) -> bool:
    needle = str(path)
    slash_needle = re.sub(r"/+", "/", needle.replace("\\", "/"))
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    variants = [text]
    for _ in range(4):
        next_text = variants[-1].replace("\\\\", "\\")
        if next_text == variants[-1]:
            break
        variants.append(next_text)
    return any(
        needle in candidate
        or slash_needle in re.sub(r"/+", "/", candidate.replace("\\", "/"))
        for candidate in variants
    )


def direct_artifact_tool_calls(parsed: dict[str, Any], output_path: Path) -> list[dict[str, Any]]:
    calls = []
    for event in parsed["events"]:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "mcp_tool_call":
            continue
        if not contains_path_reference(item.get("arguments"), output_path):
            continue
        calls.append(
            {
                "id": item.get("id"),
                "server": item.get("server"),
                "tool": item.get("tool"),
                "title": (item.get("arguments") or {}).get("title") if isinstance(item.get("arguments"), dict) else None,
                "status": item.get("status"),
            }
        )
    return calls


def expected_level2_artifact_text(case: dict[str, Any], output_path: Path, sentinel: str) -> str:
    artifact_stem = output_path.stem
    case_name = re.sub(r"\.artifact(?:-r\d+)?$", "", artifact_stem)
    return (
        f"case: {case_name}\n"
        f"model: {case['model'].split('/', 1)[-1]}\n"
        f"endpoint: {case['endpoint']}\n"
        f"{sentinel}\n"
        "artifact: ok\n"
    )


def analyze_level2(case: dict[str, Any], output_path: Path, sentinel: str) -> dict[str, Any]:
    stdout_path = Path(case["stdout"])
    stderr_path = Path(case["stderr"])
    parsed = parse_cli_events(stdout_path)
    spawns = completed_tool_calls(parsed, {"spawn_agent"})
    waits = completed_tool_calls(parsed, {"wait", "wait_agent"})
    closes = completed_tool_calls(parsed, {"close_agent"})
    roles = [role for role in (role_from_prompt(spawn.get("prompt")) for spawn in spawns) if role]
    direct_artifact_commands = []
    for event in parsed["events"]:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            command = item.get("command")
            if isinstance(command, str) and str(output_path) in command:
                direct_artifact_commands.append(command)
    direct_artifact_mcp_calls = direct_artifact_tool_calls(parsed, output_path)
    expected_content = expected_level2_artifact_text(case, output_path, sentinel)
    artifact_text = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
    normalized_artifact_text = artifact_text.lstrip("\ufeff")
    final_lines = [line.strip() for line in parsed["final_text"].splitlines() if line.strip()]
    router = router_errors(parsed, stderr_path)
    proxy_counts = proxy_event_counts_for_case(case, parsed)
    pass_checks = {
        "exit_code_zero": case.get("exit_code") == 0,
        "not_timed_out": not case.get("timed_out"),
        "has_implementer": "implementer" in roles,
        "has_spec_reviewer": "spec-reviewer" in roles,
        "has_quality_reviewer": "quality-reviewer" in roles,
        "role_order": level2_role_order_valid(roles),
        "role_lifecycle_order": level2_role_lifecycle_order_valid(parsed),
        "has_waits": len(waits) >= 3,
        "has_closes": len(closes) >= 3,
        "no_direct_artifact_workaround": not direct_artifact_commands and not direct_artifact_mcp_calls,
        "artifact_exact": normalized_artifact_text.rstrip("\r\n") == expected_content.rstrip("\n"),
        "final_exact": final_lines
        == [
            "RESULT: PASS",
            sentinel,
            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer",
        ],
        "no_router_errors": not router and proxy_counts["native_router_error"] == 0,
    }
    summary = {
        **case,
        **proxy_counts,
        "scenario": "level2",
        "pass": all(pass_checks.values()),
        "checks": pass_checks,
        "roles": roles,
        "sentinel": sentinel,
        "output_path": str(output_path),
        "artifact_text": artifact_text,
        "expected_artifact_text": expected_content,
        "direct_artifact_commands": direct_artifact_commands,
        "direct_artifact_mcp_calls": direct_artifact_mcp_calls,
        "router_errors": router,
        "tool_counts": {
            "completed_spawn": len(spawns),
            "completed_wait": len(waits),
            "completed_close": len(closes),
        },
        "final_text": parsed["final_text"],
    }
    summary["failure_classification"] = classify_failure(summary)
    summary["protocol_lock_relevant"] = False
    write_text(Path(case["stdout"]).with_suffix(".parsed.json"), json.dumps(summary, indent=2, ensure_ascii=True) + "\n")
    return summary


def level3_node_from_prompt(prompt: str | None) -> str | None:
    if not isinstance(prompt, str):
        return None
    match = re.search(r"Node:\s*(task-a-implementer|task-a-reviewer|task-b-implementer|final-summarizer)", prompt)
    return match.group(1) if match else None


def level3_dynamic_dependency_order_valid(parsed: dict[str, Any]) -> bool:
    ordered = completed_collab_events(parsed)
    spawns: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, event in ordered:
        if event.get("tool") != "spawn_agent":
            continue
        node = level3_node_from_prompt(event.get("prompt"))
        if node and node not in spawns:
            spawns[node] = (index, event)

    required = {"task-a-implementer", "task-a-reviewer", "task-b-implementer", "final-summarizer"}
    if not required.issubset(spawns):
        return False

    initial_index, initial_event = spawns["task-a-implementer"]
    reviewer_index, reviewer_event = spawns["task-a-reviewer"]
    task_b_index, task_b_event = spawns["task-b-implementer"]
    final_index, final_event = spawns["final-summarizer"]
    first_branch_index = min(reviewer_index, task_b_index)
    if not (initial_index < first_branch_index and reviewer_index < final_index and task_b_index < final_index):
        return False
    if not lifecycle_completed_between(ordered, initial_event, initial_index, first_branch_index):
        return False
    if not lifecycle_completed_between(ordered, reviewer_event, reviewer_index, final_index):
        return False
    if not lifecycle_completed_between(ordered, task_b_event, task_b_index, final_index):
        return False
    return lifecycle_completed_between(ordered, final_event, final_index, None)


def analyze_level3_dynamic_dag(case: dict[str, Any]) -> dict[str, Any]:
    stdout_path = Path(case["stdout"])
    stderr_path = Path(case["stderr"])
    parsed = parse_cli_events(stdout_path)
    spawns = completed_tool_calls(parsed, {"spawn_agent"})
    waits = completed_tool_calls(parsed, {"wait", "wait_agent"})
    closes = completed_tool_calls(parsed, {"close_agent"})
    nodes = [node for node in (level3_node_from_prompt(spawn.get("prompt")) for spawn in spawns) if node]
    final_lines = [line.strip() for line in parsed["final_text"].splitlines() if line.strip()]
    router = router_errors(parsed, stderr_path)
    proxy_counts = proxy_event_counts_for_case(case, parsed)
    transcript = all_message_text(parsed)
    expected_final = [
        "RESULT: PASS",
        "DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer",
        "DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done",
    ]
    branch_positions = [nodes.index(node) for node in ("task-a-reviewer", "task-b-implementer") if node in nodes]
    pass_checks = {
        "exit_code_zero": case.get("exit_code") == 0,
        "not_timed_out": not case.get("timed_out"),
        "initial_node_first": nodes[:1] == ["task-a-implementer"],
        "branch_nodes_seen": {"task-a-reviewer", "task-b-implementer"}.issubset(nodes),
        "final_summarizer_last": nodes[-1:] == ["final-summarizer"],
        "no_duplicate_nodes": len(nodes) == len(set(nodes)),
        "branch_after_initial": bool(branch_positions) and all(position > 0 for position in branch_positions),
        "dependency_order": level3_dynamic_dependency_order_valid(parsed),
        "worker_outputs_seen": all(token in transcript for token in ("A_DONE", "A_REVIEW_PASS", "B_DONE", "FINAL_READY")),
        "has_waits": len(waits) >= 3,
        "has_closes": len(closes) >= 4,
        "final_exact": final_lines == expected_final,
        "no_router_errors": not router and proxy_counts["native_router_error"] == 0,
    }
    summary = {
        **case,
        **proxy_counts,
        "scenario": "level3_dynamic_dag",
        "pass": all(pass_checks.values()),
        "checks": pass_checks,
        "nodes": nodes,
        "router_errors": router,
        "tool_counts": {
            "completed_spawn": len(spawns),
            "completed_wait": len(waits),
            "completed_close": len(closes),
        },
        "final_text": parsed["final_text"],
    }
    summary["failure_classification"] = classify_failure(summary)
    if not summary["pass"] and summary["failure_classification"] == "unclassified":
        summary["failure_classification"] = "dynamic_scheduler_defect"
    summary["protocol_lock_relevant"] = False
    write_text(Path(case["stdout"]).with_suffix(".parsed.json"), json.dumps(summary, indent=2, ensure_ascii=True) + "\n")
    return summary


def write_markdown_summary(run_dir: Path, summaries: list[dict[str, Any]], gateway_source: Path) -> None:
    lines = [
        "# External Model Native Subagent Level 1 + Level 2 E2E",
        "",
        f"- Run directory: `{run_dir}`",
        f"- Gateway source: `{gateway_source}`",
        f"- Generated at: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        "| Level | Case | Mode | Pass | Repair | Retry | Resample | Stream | EmptyFinal | Tool counts | Agent ids / roles | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in summaries:
        if item.get("scenario") == "level3_dynamic_dag":
            level = "Level 3"
        elif item.get("scenario") == "level2":
            level = "Level 2"
        else:
            level = "Level 1"
        name = item["case"]
        mode = item.get("subagent_mode", "")
        passed = "PASS" if item.get("pass") else "FAIL"
        repair_count = item.get("required_subagent_call_repaired", 0)
        retry_count = item.get("upstream_retry", 0)
        resample_count = item.get("lifecycle_empty_final_resample", 0)
        empty_final_count = item.get("lifecycle_empty_final_response", 0)
        stream_count = sum(
            int(item.get(key, 0) or 0)
            for key in (
                "upstream_stream_error",
                "upstream_stream_incomplete",
                "upstream_stream_interrupted",
                "upstream_stream_idle_timeout",
                "cli_stream_reconnect",
            )
        )
        counts = item.get("tool_counts", {})
        count_text = ", ".join(f"{key}={value}" for key, value in counts.items())
        ids_or_roles = ", ".join(item.get("agent_ids") or item.get("roles") or item.get("nodes") or [])
        failed = [key for key, value in (item.get("checks") or {}).items() if not value]
        reason = "ok" if not failed else ", ".join(failed)
        lines.append(
            f"| {level} | `{name}` | {mode} | {passed} | repair={repair_count} | retry={retry_count} | resample={resample_count} | stream={stream_count} | empty={empty_final_count} | {count_text} | {ids_or_roles} | {reason} |"
        )
    lines.append("")
    lines.append("Raw provider probe results are diagnostic-only and are not included in these pass/fail rows.")
    write_text(run_dir / "summary.md", "\n".join(lines) + "\n")


def append_progress(run_dir: Path, **fields: Any) -> None:
    payload = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}
    with _PROGRESS_LOCK:
        with (run_dir / "progress.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")


def run_e2e_task(run_dir: Path, port: int, task: dict[str, Any], ephemeral_cli: bool) -> dict[str, Any]:
    append_progress(
        run_dir,
        case=task["case_name"],
        status="started",
        model=task["model_id"],
        subagent_mode=task["subagent_mode"],
    )
    case = run_codex_case(
        run_dir=run_dir,
        case_name=task["case_name"],
        prompt=task["prompt"],
        model_id=task["model_id"],
        port=port,
        timeout=task["timeout"],
        ephemeral_cli=ephemeral_cli,
        preserve_cli_tools=task["preserve_cli_tools"],
        subagent_mode=task["subagent_mode"],
        main_retry_attempts=task["main_retry_attempts"],
    )
    case["endpoint"] = task["endpoint"]
    case["repeat_index"] = task.get("repeat_index")
    case["repeat_count"] = task.get("repeat_count")
    if task["scenario"] == "level2":
        summary = analyze_level2(case, task["output_path"], task["sentinel"])
    elif task["scenario"] == "level3_dynamic_dag":
        summary = analyze_level3_dynamic_dag(case)
    else:
        summary = analyze_level1(case, task["scenario"], task["sentinels"])
    append_progress(run_dir, case=task["case_name"], status="finished", passed=summary["pass"])
    return summary


def repeated_tasks(tasks: list[dict[str, Any]], repeat: int) -> list[dict[str, Any]]:
    repeat_count = max(1, repeat)
    if repeat_count == 1:
        return tasks
    expanded: list[dict[str, Any]] = []
    for task in tasks:
        for index in range(1, repeat_count + 1):
            copy = dict(task)
            copy["case_name"] = f"{task['case_name']}-r{index:02d}"
            copy["repeat_index"] = index
            copy["repeat_count"] = repeat_count
            if "output_path" in copy:
                output_path = Path(copy["output_path"])
                repeated_output_path = output_path.with_name(f"{output_path.stem}-r{index:02d}{output_path.suffix}")
                copy["output_path"] = repeated_output_path
                if isinstance(copy.get("prompt"), str):
                    copy["prompt"] = copy["prompt"].replace(str(output_path), str(repeated_output_path))
            expanded.append(copy)
    return expanded


def run_e2e_tasks(
    run_dir: Path,
    port: int,
    tasks: list[dict[str, Any]],
    jobs: int,
    ephemeral_cli: bool,
) -> list[dict[str, Any]]:
    if not tasks:
        return []
    if jobs <= 1 or len(tasks) == 1:
        return [run_e2e_task(run_dir, port, task, ephemeral_cli) for task in tasks]

    summaries_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {
            executor.submit(run_e2e_task, run_dir, port, task, ephemeral_cli): index
            for index, task in enumerate(tasks)
        }
        for future in as_completed(futures):
            summaries_by_index[futures[future]] = future.result()
    return [summaries_by_index[index] for index in range(len(tasks))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--level", choices=["level1", "level2", "level3", "all"], default="all")
    parser.add_argument("--models", default="", help="Comma-separated short model names to run, e.g. glm52,k2_7,m3.")
    parser.add_argument("--endpoints", default="", help="Comma-separated endpoints to run: responses,chat.")
    parser.add_argument("--scenarios", default="", help="Comma-separated Level 1 scenarios to run: single,two.")
    parser.add_argument("--workflow", choices=["dynamic-dag"], default="dynamic-dag")
    parser.add_argument("--level1-timeout", type=int, default=420)
    parser.add_argument("--level2-timeout", type=int, default=720)
    parser.add_argument("--level3-timeout", type=int, default=720)
    parser.add_argument("--jobs", type=int, default=1, help="Maximum number of independent E2E cases to run concurrently.")
    parser.add_argument(
        "--subagent-mode",
        choices=["strict", "guided", "assisted"],
        default="assisted",
        help="Subagent Gateway behavior: strict=protocol only, guided=state hints only, assisted=state hints plus semantic repair.",
    )
    parser.add_argument("--repeat", type=int, default=1, help="Repeat each selected case this many times.")
    parser.add_argument(
        "--main-retry-attempts",
        type=int,
        default=3,
        help="CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS for Gateway main generation requests.",
    )
    parser.add_argument(
        "--upstream-base-url",
        default=os.environ.get("SUBAGENT_E2E_UPSTREAM_BASE_URL", "https://ollama.com/v1"),
        help="Upstream provider base URL used by the temporary E2E Gateway providers.",
    )
    parser.add_argument(
        "--ephemeral-cli",
        action="store_true",
        help="Use codex exec --ephemeral for diagnostic comparison. Default uses persistent CLI sessions.",
    )
    parser.add_argument(
        "--minimal-cli-tools",
        action="store_true",
        help="Disable user agent/plugin tool config. Default preserves normal CLI tools such as node_repl for Level 2.",
    )
    args = parser.parse_args()
    selected_models = {item.strip() for item in args.models.split(",") if item.strip()}
    selected_endpoints = {item.strip() for item in args.endpoints.split(",") if item.strip()}
    selected_scenarios = {item.strip() for item in args.scenarios.split(",") if item.strip()}

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = DEFAULT_OUT_ROOT / f"level12-e2e-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    port = args.port or free_port()
    summaries: list[dict[str, Any]] = []
    gateway_proc: subprocess.Popen[bytes] | None = None
    try:
        write_text(run_dir / "upstream-base-url.txt", args.upstream_base_url.rstrip("/") + "\n")
        gateway_proc, _ = start_gateway(
            run_dir,
            port,
            args.upstream_base_url,
            args.subagent_mode,
            args.main_retry_attempts,
        )
        gateway_source = REPO / "src-python" / "codex_proxy.py"
        cases: list[tuple[str, str, str, str, str]] = []
        for short_model, model in MODELS:
            if selected_models and short_model not in selected_models:
                continue
            for endpoint, provider, _protocol in ENDPOINTS:
                if selected_endpoints and endpoint not in selected_endpoints:
                    continue
                model_id = f"{provider}/{model}"
                cases.append((short_model, model, endpoint, provider, model_id))

        if args.level in {"level1", "all"}:
            level1_tasks: list[dict[str, Any]] = []
            for short_model, model, endpoint, _provider, model_id in cases:
                if not selected_scenarios or "single" in selected_scenarios:
                    single_sentinel = f"SENTINEL:level1-single-{short_model}-{endpoint}-20260706"
                    single_name = f"level1-{short_model}-{endpoint}-single"
                    level1_tasks.append(
                        {
                            "case_name": single_name,
                            "prompt": level1_single_prompt(single_sentinel),
                            "model_id": model_id,
                            "endpoint": endpoint,
                            "timeout": args.level1_timeout,
                            "scenario": "single",
                            "sentinels": [single_sentinel],
                            "preserve_cli_tools": not args.minimal_cli_tools,
                            "subagent_mode": args.subagent_mode,
                            "main_retry_attempts": args.main_retry_attempts,
                        }
                    )

                if not selected_scenarios or "two" in selected_scenarios:
                    sentinel_a = f"SENTINEL:level1-two-a-{short_model}-{endpoint}-20260706"
                    sentinel_b = f"SENTINEL:level1-two-b-{short_model}-{endpoint}-20260706"
                    two_name = f"level1-{short_model}-{endpoint}-two"
                    level1_tasks.append(
                        {
                            "case_name": two_name,
                            "prompt": level1_two_prompt(sentinel_a, sentinel_b),
                            "model_id": model_id,
                            "endpoint": endpoint,
                            "timeout": args.level1_timeout,
                            "scenario": "two",
                            "sentinels": [sentinel_a, sentinel_b],
                            "preserve_cli_tools": not args.minimal_cli_tools,
                            "subagent_mode": args.subagent_mode,
                            "main_retry_attempts": args.main_retry_attempts,
                        }
                    )
            level1_tasks = repeated_tasks(level1_tasks, args.repeat)
            summaries.extend(run_e2e_tasks(run_dir, port, level1_tasks, args.jobs, args.ephemeral_cli))

        level1_ok = all(item.get("pass") for item in summaries if item.get("scenario") in {"single", "two"})
        if args.level == "level2" or (args.level == "all" and level1_ok):
            level2_tasks: list[dict[str, Any]] = []
            for short_model, model, endpoint, _provider, model_id in cases:
                case_name = f"level2-{short_model}-{endpoint}"
                output_path = run_dir / f"{case_name}.artifact.txt"
                sentinel = f"SENTINEL:level2-{short_model}-{endpoint}-20260706"
                baseline_status = git_status_baseline()
                level2_tasks.append(
                    {
                        "case_name": case_name,
                        "prompt": level2_prompt(case_name, model, endpoint, output_path, sentinel, baseline_status),
                        "model_id": model_id,
                        "endpoint": endpoint,
                        "timeout": args.level2_timeout,
                        "scenario": "level2",
                        "output_path": output_path,
                        "sentinel": sentinel,
                        "preserve_cli_tools": not args.minimal_cli_tools,
                        "subagent_mode": args.subagent_mode,
                        "main_retry_attempts": args.main_retry_attempts,
                    }
                )
            level2_tasks = repeated_tasks(level2_tasks, args.repeat)
            summaries.extend(run_e2e_tasks(run_dir, port, level2_tasks, args.jobs, args.ephemeral_cli))
        elif args.level == "all":
            write_text(run_dir / "level2.skipped.txt", "Level 2 skipped because Level 1 did not pass cleanly.\n")

        level2_ok = all(item.get("pass") for item in summaries if item.get("scenario") == "level2")
        if args.level == "level3" or (args.level == "all" and level1_ok and level2_ok):
            level3_tasks: list[dict[str, Any]] = []
            for short_model, _model, endpoint, _provider, model_id in cases:
                case_name = f"level3-{short_model}-{endpoint}"
                level3_tasks.append(
                    {
                        "case_name": case_name,
                        "prompt": level3_dynamic_dag_prompt(case_name),
                        "model_id": model_id,
                        "endpoint": endpoint,
                        "timeout": args.level3_timeout,
                        "scenario": "level3_dynamic_dag",
                        "preserve_cli_tools": not args.minimal_cli_tools,
                        "subagent_mode": args.subagent_mode,
                        "main_retry_attempts": args.main_retry_attempts,
                    }
                )
            level3_tasks = repeated_tasks(level3_tasks, args.repeat)
            summaries.extend(run_e2e_tasks(run_dir, port, level3_tasks, args.jobs, args.ephemeral_cli))
        elif args.level == "all":
            write_text(run_dir / "level3.skipped.txt", "Level 3 skipped because prior levels did not pass cleanly.\n")

        write_text(run_dir / "summary.json", json.dumps(summaries, indent=2, ensure_ascii=True) + "\n")
        write_markdown_summary(run_dir, summaries, gateway_source)
        print(str(run_dir))
        return 0 if summaries and all(item.get("pass") for item in summaries) else 1
    finally:
        if gateway_proc is not None:
            stop_gateway(gateway_proc, port)


if __name__ == "__main__":
    raise SystemExit(main())
