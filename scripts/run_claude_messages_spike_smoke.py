"""Run a credential-free Claude Code -> in-memory Messages prototype smoke.

The harness is intentionally outside the Gateway handler.  It starts a local
loopback-only server, accepts a throwaway fixture credential, converts synthetic
Responses events through the Issue #74 prototype, and persists only a
structural/sanitized trace.  It never reads or writes a real provider credential.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src-python"))

from anthropic_messages_spike import (  # noqa: E402
    classify_claude_headers,
    messages_to_responses,
    responses_events_to_messages_sse,
)


FIXTURE_TOOL_ID = "toolu_smoke_read_001"


def _text_response_events(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": {"id": "resp_smoke_text", "model": "fixture-model"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_smoke_text", "type": "message", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "output_index": 0, "delta": text},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": "msg_smoke_text", "type": "message", "role": "assistant", "content": []},
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_smoke_text",
                "model": "fixture-model",
                "usage": {"input_tokens": 11, "output_tokens": 2},
            },
        },
    ]


def _tool_response_events() -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": {"id": "resp_smoke_tool", "model": "fixture-model"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "fc_smoke_read",
                "type": "function_call",
                "call_id": FIXTURE_TOOL_ID,
                "name": "Read",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"file_path":"fixture.txt"}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "fc_smoke_read",
                "type": "function_call",
                "call_id": FIXTURE_TOOL_ID,
                "name": "Read",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_smoke_tool",
                "model": "fixture-model",
                "usage": {"input_tokens": 13, "output_tokens": 3},
            },
        },
    ]


def _content_block_types(value: Any) -> list[str]:
    if isinstance(value, str):
        return ["text"]
    if not isinstance(value, list):
        return ["<invalid>"]
    return [str(item.get("type", "<missing>")) for item in value if isinstance(item, Mapping)]


def _shape(value: Any, *, depth: int = 0) -> Any:
    """Return key/type structure only; never retain a request value."""

    if depth >= 2:
        return "<nested>"
    if isinstance(value, Mapping):
        return {"keys": sorted(str(key) for key in value)}
    if isinstance(value, list):
        return [_shape(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return "<string>"
    if isinstance(value, bool):
        return "<boolean>"
    if isinstance(value, int | float):
        return "<number>"
    if value is None:
        return "<null>"
    return f"<{type(value).__name__}>"


class SmokeScenario:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.request_received = threading.Event()
        self.stream_started = threading.Event()
        self.complete = threading.Event()
        self.request_summaries: list[dict[str, Any]] = []
        self._step = 0
        self.tool_follow_up_verified = False
        self.disconnect_observed = False
        self.lock = threading.Lock()

    def summarize_request(self, body: Mapping[str, Any], headers: Mapping[str, str], path: str) -> None:
        policy = classify_claude_headers(headers, upstream_format="responses")
        messages = body.get("messages")
        message_summaries: list[dict[str, Any]] = []
        tool_result_ids: list[str] = []
        tool_result_is_first_content_block = False
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, Mapping):
                    continue
                content = item.get("content")
                message_summaries.append(
                    {
                        "role": item.get("role") if isinstance(item.get("role"), str) else "<invalid>",
                        "content_block_types": _content_block_types(content),
                    }
                )
                if isinstance(content, list):
                    if (
                        item.get("role") == "user"
                        and content
                        and isinstance(content[0], Mapping)
                        and content[0].get("type") == "tool_result"
                    ):
                        tool_result_is_first_content_block = True
                    tool_result_ids.extend(
                        str(block.get("tool_use_id"))
                        for block in content
                        if isinstance(block, Mapping)
                        and block.get("type") == "tool_result"
                        and isinstance(block.get("tool_use_id"), str)
                    )
        translated = messages_to_responses(body)
        summary = {
            "path": path.split("?", 1)[0],
            "header_policy": {
                "consumed": list(policy.consumed),
                "unsupported": list(policy.unsupported),
                "sanitized": policy.sanitized,
            },
            "request_keys": sorted(str(key) for key in body),
            "selected_field_shapes": {
                key: _shape(body[key])
                for key in ("metadata", "thinking", "output_config", "system", "tools")
                if key in body
            },
            "message_history": message_summaries,
            "messages_to_responses_forwardable": translated.forwardable,
            "messages_to_responses_unsupported": list(translated.unsupported),
            "contains_expected_tool_result": FIXTURE_TOOL_ID in tool_result_ids,
            "tool_result_is_first_content_block": tool_result_is_first_content_block,
        }
        with self.lock:
            self.request_summaries.append(summary)
            if FIXTURE_TOOL_ID in tool_result_ids:
                self.tool_follow_up_verified = True

    def response_records(self) -> tuple[bytes, ...]:
        with self.lock:
            step = self._step
            self._step += 1
        if self.kind == "tool" and step == 0:
            return responses_events_to_messages_sse(_tool_response_events())
        if self.kind == "tool":
            return responses_events_to_messages_sse(_text_response_events("SPIKE_TOOL_OK"))
        if self.kind == "cancel":
            return responses_events_to_messages_sse(_text_response_events("SPIKE_CANCEL_PENDING"))
        return responses_events_to_messages_sse(_text_response_events("SPIKE_TEXT_OK"))


def _make_handler(scenario: SmokeScenario):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/v1/messages"):
                self.send_response(404)
                self.end_headers()
                return
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            if not isinstance(body, Mapping):
                self.send_response(400)
                self.end_headers()
                return
            scenario.summarize_request(body, {name: value for name, value in self.headers.items()}, self.path)
            scenario.request_received.set()
            records = scenario.response_records()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for index, record in enumerate(records):
                    self.wfile.write(record)
                    self.wfile.flush()
                    if scenario.kind == "cancel" and index == 2:
                        scenario.stream_started.set()
                        time.sleep(2)
            except (BrokenPipeError, ConnectionResetError):
                scenario.disconnect_observed = True
            finally:
                scenario.complete.set()

    return Handler


def _safe_environment(base_url: str, config_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("ANTHROPIC_") or name.startswith("CLAUDE_CODE_"):
            env.pop(name, None)
    env.update(
        {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": "fixture-only-gateway-credential",
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env


def _command(cli: str, kind: str) -> tuple[list[str], str]:
    if kind == "tool":
        expected = "SPIKE_TOOL_OK"
        prompt = "Use the Read tool on fixture.txt, then return the tool result."
        return (
            [
                cli,
                "--bare",
                "--print",
                prompt,
                "--output-format",
                "text",
                "--no-session-persistence",
                "--system-prompt",
                "Protocol fixture client. Use only the requested fixture tool.",
                "--model",
                "sonnet",
                "--tools",
                "Read",
                "--allow-dangerously-skip-permissions",
                "--permission-mode",
                "bypassPermissions",
            ],
            expected,
        )
    expected = "SPIKE_TEXT_OK"
    prompt = "Return exactly SPIKE_TEXT_OK."
    return (
        [
            cli,
            "--bare",
            "--print",
            prompt,
            "--output-format",
            "text",
            "--no-session-persistence",
            "--system-prompt",
            "Protocol fixture client. Return only the requested marker.",
            "--model",
            "sonnet",
            "--tools",
            "",
        ],
        expected,
    )


def _run_scenario(cli: str, kind: str) -> dict[str, Any]:
    scenario = SmokeScenario(kind)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(scenario))
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="codexhub-claude-spike-") as temporary:
            temporary_path = Path(temporary)
            (temporary_path / "fixture.txt").write_text("SPIKE_FIXTURE_FILE", encoding="utf-8")
            command, expected = _command(cli, kind)
            environment = _safe_environment(f"http://127.0.0.1:{server.server_port}", temporary_path / "config")
            if kind == "cancel":
                process = subprocess.Popen(
                    command,
                    cwd=temporary_path,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stream_started = scenario.stream_started.wait(timeout=30)
                if stream_started:
                    process.terminate()
                else:
                    process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=10)
                scenario.complete.wait(timeout=5)
                client = {
                    "returncode": process.returncode,
                    "expected_output_seen": expected in stdout,
                    "stdout_length": len(stdout),
                    "stderr_length": len(stderr),
                    "forced_process_termination": True,
                }
            else:
                completed = subprocess.run(
                    command,
                    cwd=temporary_path,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                client = {
                    "returncode": completed.returncode,
                    "expected_output_seen": expected in completed.stdout,
                    "stdout_length": len(completed.stdout),
                    "stderr_length": len(completed.stderr),
                }
        translation_forwardable = all(
            item["messages_to_responses_forwardable"] for item in scenario.request_summaries
        )
        if kind == "tool":
            passed = (
                client["returncode"] == 0
                and client["expected_output_seen"]
                and scenario.tool_follow_up_verified
                and translation_forwardable
            )
        elif kind == "cancel":
            passed = scenario.stream_started.is_set() and scenario.disconnect_observed
        else:
            passed = client["returncode"] == 0 and client["expected_output_seen"] and translation_forwardable
        return {
            "scenario": kind,
            "passed": passed,
            "client": client,
            "request_summaries": scenario.request_summaries,
            "tool_follow_up_verified": scenario.tool_follow_up_verified,
            "stream_started": scenario.stream_started.is_set(),
            "disconnect_observed": scenario.disconnect_observed,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"scenario": kind, "passed": False, "blocker": type(exc).__name__}
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def _version(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    version = completed.stdout.strip()
    return version if completed.returncode == 0 and version else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default="claude", help="Claude Code executable name or path")
    parser.add_argument(
        "--scenarios",
        choices=("text", "tool", "cancel", "all"),
        default="all",
        help="Which safe local-loopback smoke scenarios to run",
    )
    parser.add_argument(
        "--write-trace",
        type=Path,
        default=REPO_ROOT / "tests" / "fixtures" / "claude_messages_real_cli_smoke.json",
        help="Where to write the sanitized structural result",
    )
    parser.add_argument(
        "--require-full",
        action="store_true",
        help="Exit nonzero when the captured evidence is scoped PARTIAL rather than fully forwardable",
    )
    args = parser.parse_args()
    cli_version = _version([args.cli, "--version"])
    node_version = _version(["node", "--version"])
    scenarios = ("text", "tool", "cancel") if args.scenarios == "all" else (args.scenarios,)
    results = [_run_scenario(args.cli, scenario) for scenario in scenarios]
    capture_succeeded = all(
        isinstance(item.get("request_summaries"), list) and bool(item["request_summaries"])
        for item in results
    )
    full_compatibility = all(item.get("passed") for item in results)
    trace = {
        "fixture_schema": "codexhub-claude-messages-real-smoke/v1",
        "sanitized": True,
        "capture_kind": "real_cli_to_local_loopback_prototype",
        "local_cli_version": cli_version,
        "node_version": node_version,
        "platform": platform.system(),
        "capture_succeeded": capture_succeeded,
        "full_compatibility": full_compatibility,
        "compatibility_outcome": "GO" if full_compatibility else "scoped PARTIAL",
        "scenarios": results,
        "cancellation_interpretation": (
            "Forced process termination is an attempted downstream disconnect only; "
            "it does not prove Ctrl+C/Escape wire semantics."
        ),
    }
    args.write_trace.parent.mkdir(parents=True, exist_ok=True)
    args.write_trace.write_text(json.dumps(trace, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "written": args.write_trace.name,
                "capture_succeeded": capture_succeeded,
                "full_compatibility": full_compatibility,
            },
            indent=2,
        )
    )
    if not capture_succeeded:
        return 1
    return 0 if full_compatibility or not args.require_full else 1


if __name__ == "__main__":
    raise SystemExit(main())
