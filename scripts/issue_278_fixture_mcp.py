#!/usr/bin/env python3
"""Tiny stdio MCP server used only by the issue #278 CLI runner.

It deliberately exposes one deterministic tool and returns bounded opaque
content.  The runner never records this process' messages.
"""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import json
import os
import sys


def _ledger_path() -> str | None:
    for index, value in enumerate(sys.argv[1:]):
        if value == "--ledger" and index + 2 <= len(sys.argv[1:]):
            candidate = sys.argv[index + 2]
            if candidate:
                return candidate
    return os.environ.get("ISSUE_278_MCP_LEDGER")


def _record(method: str) -> None:
    path = _ledger_path()
    if not path:
        return
    try:
        try:
            current = json.loads(open(path, encoding="utf-8").read())
        except (OSError, UnicodeError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        key = "tools_list_count" if method == "tools/list" else "tools_call_count"
        current[key] = int(current.get(key, 0)) + 1
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(current, stream, separators=(",", ":"))
    except (OSError, TypeError, ValueError):
        return


def _reply(message_id: object, result: dict) -> None:
    payload = {"jsonrpc": "2.0", "id": message_id, "result": result}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        message_id = request.get("id")
        if method == "initialize":
            _reply(
                message_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "issue-278-fixture", "version": "1"},
                },
            )
        elif method == "tools/list":
            _record(method)
            _reply(
                message_id,
                {
                    "tools": [
                        {
                            "name": "fixture_discovered_tool",
                            "description": "Issue 278 protocol fixture tool.",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            )
        elif method == "tools/call":
            _record(method)
            _reply(message_id, {"content": [{"type": "text", "text": "FIXTURE_TOOL_RESULT"}], "isError": False})
        elif message_id is not None:
            _reply(message_id, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
