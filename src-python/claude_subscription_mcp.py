"""Private stdio MCP endpoint: declare caller tools, never execute them.

The official CLI sends actual calls to a request-local authenticated loopback
endpoint. That endpoint deliberately never returns a tool result: the Gateway
ends this CLI turn after forwarding its requests to the caller. A later request
carries the caller's real results in complete conversation history.
"""
from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
from pathlib import Path
import sys
import threading
from typing import Any, Mapping, TextIO
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

_MAX_LINE = 4 * 1024 * 1024


def run_mcp(input_stream: TextIO, output_stream: TextIO, tools: list[dict[str, Any]],
            callback: str, *, stop: threading.Event | None = None) -> None:
    """Serve the restricted MCP contract, without any success-shaped result.

    Stream and stop ports make the real stdio protocol independently testable.
    Only an authenticated request-local parent can receive call arguments.
    """
    url = urlsplit(callback)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.query or url.fragment:
        raise ValueError("invalid private MCP callback")
    names = {tool["name"] for tool in tools}
    stopped = stop or threading.Event()
    output_lock = threading.Lock()

    def respond(request_id: Any, *, result: Any = None, error: str | None = None) -> None:
        value = {"jsonrpc": "2.0", "id": request_id}
        if error is None:
            value["result"] = result
        else:
            value["error"] = {"code": -32602, "message": error}
        with output_lock:
            output_stream.write(json.dumps(value, separators=(",", ":")) + "\n")
            output_stream.flush()

    def call(request_id: Any, params: Mapping[str, Any]) -> None:
        meta = params.get("_meta")
        call_id = meta.get("claudecode/toolUseId") if isinstance(meta, Mapping) else None
        arguments = params.get("arguments")
        name = params.get("name")
        if name not in names or not isinstance(call_id, str) or not call_id or not isinstance(arguments, dict):
            respond(request_id, error="invalid caller tool request")
            return
        data = json.dumps({"id": call_id, "name": name, "arguments": arguments}, separators=(",", ":")).encode()
        try:
            # No inherited proxy; no redirects to a different endpoint.
            with build_opener(ProxyHandler({})).open(Request(callback, data=data,
                    headers={"Content-Type": "application/json"}, method="POST"), timeout=3600) as response:
                response.read(1024)
        except Exception:
            # Termination, cancellation, and turn boundaries return no MCP
            # result. In particular, they are never reported as tool success.
            return
        stopped.wait()

    while not stopped.is_set():
        line = input_stream.readline(_MAX_LINE + 1)
        if not line:
            return
        if len(line) > _MAX_LINE:
            raise ValueError("MCP request exceeds limit")
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        request_id = request["id"]
        method = request.get("method")
        if method == "initialize":
            respond(request_id, result={"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                       "serverInfo": {"name": "codexhub-caller-tools", "version": "1"}})
        elif method == "tools/list":
            respond(request_id, result={"tools": tools})
        elif method == "tools/call" and isinstance(request.get("params"), dict):
            threading.Thread(target=call, args=(request_id, request["params"]), daemon=True).start()
        else:
            respond(request_id, error="unsupported MCP method")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("callback")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    if len(raw) > _MAX_LINE:
        raise ValueError("MCP manifest exceeds limit")
    tools = json.loads(raw)
    if not isinstance(tools, list) or any(not isinstance(row, dict) for row in tools):
        raise ValueError("invalid MCP manifest")
    run_mcp(sys.stdin, sys.stdout, tools, args.callback)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
