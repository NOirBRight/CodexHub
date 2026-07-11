"""Drive the App-managed codex app-server through a local CodexHub Gateway."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--session-jsonl", type=Path)
    parser.add_argument("--tool-calls", type=int, default=0)
    parser.add_argument("--pause-between-turns", type=float, default=0.0)
    args = parser.parse_args()

    env = os.environ.copy()
    env["CODEX_HOME"] = str(args.home.resolve())
    command = [
        str(args.codex.resolve()),
        "-c",
        "features.code_mode_host=true",
        "app-server",
        "--analytics-default-enabled",
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=args.cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    stdout_lines: queue.Queue[dict[str, object] | None] = queue.Queue()
    stderr_lines: list[str] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                stdout_lines.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        stdout_lines.put(None)

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_lines.append(line.rstrip())

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    next_id = 1
    pending: dict[int, dict[str, object]] = {}

    def send(method: str, params: dict[str, object] | None = None, *, notification: bool = False) -> int | None:
        nonlocal next_id
        payload: dict[str, object] = {"method": method}
        request_id: int | None = None
        if not notification:
            request_id = next_id
            next_id += 1
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()
        return request_id

    def receive_until(predicate, timeout: float) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = stdout_lines.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if message is None:
                break
            message_id = message.get("id")
            if predicate(message):
                return message
            if isinstance(message_id, int) and "method" not in message:
                pending[message_id] = message
            method = message.get("method")
            if isinstance(message_id, int) and isinstance(method, str):
                response = {
                    "id": message_id,
                    "error": {"code": -32000, "message": f"probe does not approve {method}"},
                }
                assert process.stdin is not None
                process.stdin.write(json.dumps(response, separators=(",", ":")) + "\n")
                process.stdin.flush()
        tail = "\n".join(stderr_lines[-30:])
        raise TimeoutError(f"app-server timed out/exited (code={process.poll()}):\n{tail}")

    def wait_response(request_id: int, timeout: float = 30.0) -> dict[str, object]:
        if request_id in pending:
            return pending.pop(request_id)
        return receive_until(lambda message: message.get("id") == request_id and "method" not in message, timeout)

    try:
        initialize_id = send(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_desktop",
                    "title": "Codex Desktop",
                    "version": "0.144.0-alpha.4",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                    "optOutNotificationMethods": [],
                },
            },
        )
        assert initialize_id is not None
        initialize = wait_response(initialize_id)
        if "error" in initialize:
            raise RuntimeError(f"initialize failed: {initialize['error']}")
        send("initialized", notification=True)

        thread_params: dict[str, object] = {
            "cwd": str(args.cwd.resolve()),
            "model": "gpt-5.6-sol",
            "modelProvider": "custom",
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "sessionStartSource": "startup",
        }
        if args.session_jsonl:
            with args.session_jsonl.open("r", encoding="utf-8") as handle:
                session_meta = json.loads(handle.readline()).get("payload", {})
            dynamic_tools = session_meta.get("dynamic_tools")
            if isinstance(dynamic_tools, list):
                thread_params["dynamicTools"] = dynamic_tools
            base_instructions = session_meta.get("base_instructions")
            if isinstance(base_instructions, dict) and isinstance(base_instructions.get("text"), str):
                thread_params["baseInstructions"] = base_instructions["text"]

        thread_id_request = send("thread/start", thread_params)
        assert thread_id_request is not None
        thread_response = wait_response(thread_id_request)
        if "error" in thread_response:
            raise RuntimeError(f"thread/start failed: {thread_response['error']}")
        result = thread_response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise RuntimeError(f"thread/start returned no thread id: {thread_response}")
        print(json.dumps({"event": "thread_started", "thread_id": thread_id}), flush=True)

        durations: list[float] = []
        for index in range(1, args.turns + 1):
            if index > 1 and args.pause_between_turns > 0:
                time.sleep(args.pause_between_turns)
            started = time.monotonic()
            if args.tool_calls:
                prompt = (
                    f"Run `git rev-parse --short HEAD` exactly {args.tool_calls} times, using one separate "
                    "shell tool call each time. Do not combine calls. Then report the final hash only."
                )
            else:
                prompt = f"Transport probe {index}/{args.turns}. Reply with exactly OK. Do not call tools."
            turn_request = send(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                },
            )
            assert turn_request is not None
            turn_response = wait_response(turn_request)
            if "error" in turn_response:
                raise RuntimeError(f"turn/start {index} failed: {turn_response['error']}")
            turn_result = turn_response.get("result")
            started_turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            turn_id = started_turn.get("id") if isinstance(started_turn, dict) else None
            if not isinstance(turn_id, str):
                raise RuntimeError(f"turn/start {index} returned no turn id: {turn_response}")
            completed = receive_until(
                lambda message: message.get("method") == "turn/completed"
                and isinstance(message.get("params"), dict)
                and message["params"].get("threadId") == thread_id
                and isinstance(message["params"].get("turn"), dict)
                and message["params"]["turn"].get("id") == turn_id,
                args.timeout,
            )
            completed_params = completed.get("params")
            completed_turn = completed_params.get("turn") if isinstance(completed_params, dict) else None
            completed_status = completed_turn.get("status") if isinstance(completed_turn, dict) else None
            if completed_status != "completed":
                raise RuntimeError(f"turn {index} completed with status {completed_status}: {completed}")
            duration = time.monotonic() - started
            durations.append(duration)
            print(json.dumps({"event": "turn_completed", "turn": index, "duration_seconds": round(duration, 3)}), flush=True)

        print(
            json.dumps(
                {
                    "event": "probe_completed",
                    "turns": len(durations),
                    "max_duration_seconds": round(max(durations, default=0.0), 3),
                    "slow_turns": sum(duration > 15 for duration in durations),
                }
            ),
            flush=True,
        )
        return 0
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
