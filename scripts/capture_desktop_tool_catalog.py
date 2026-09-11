"""Capture Desktop tool schemas without sending prompts to any provider.

Uses an isolated Codex home, a loopback capture endpoint, and the current
Desktop tools/list pipe. Only tool declarations are retained in the output.
"""

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import socket
import struct
import sys
import os
import json
import tempfile
import shutil
import threading
import subprocess
import gzip
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_issue_106_task_lifecycle import (
    JsonRpcClient,
    response_result,
    stop_issue106_app_server,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(15)
        connection.connect(os.environ["CODEX_APP_TOOLS_PIPE_PATH"])
        data = json.dumps(
            {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {"threadStartKind": "all"},
            }
        ).encode()
        connection.sendall(struct.pack("<I", len(data)) + data)

        def read_frame_bytes(n):
            data = b""
            while len(data) < n:
                chunk = connection.recv(n - len(data))
                if not chunk:
                    raise RuntimeError("Desktop tools pipe closed")
                data += chunk
            return data

        size = struct.unpack("<I", read_frame_bytes(4))[0]
        if size > 16 * 1024 * 1024:
            raise RuntimeError("Desktop catalog exceeds bound")
        app_tools = json.loads(read_frame_bytes(size))["result"]["tools"]
    captured = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            b = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.headers.get("Content-Encoding") == "gzip":
                b = gzip.decompress(b)
            p = json.loads(b)
            tools = p.get("tools", [])
            if tools:
                tools.append(
                    {
                        "type": "namespace",
                        "name": "mcp__codex_app",
                        "description": "Tools provided by the Codex app.",
                        "tools": [
                            {
                                "type": "function",
                                "name": a["name"],
                                "description": a["description"],
                                "parameters": a["inputSchema"],
                            }
                            for a in app_tools
                        ],
                    }
                )
                args.output.write_text(json.dumps(tools, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "keys": list(p),
                        "model": p.get("model"),
                        "capture_tools": len(tools),
                        "expanded_count": sum(
                            len(t.get("tools", []))
                            if t.get("type") == "namespace"
                            else 1
                            for t in tools
                        ),
                        "types": sorted(set(t.get("type", "") for t in tools)),
                    }
                ),
                flush=True,
            )
            out = json.dumps(
                {
                    "error": {
                        "message": "tool catalog capture complete",
                        "type": "invalid_request_error",
                    }
                }
            ).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            if tools:
                captured.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="codexhub-tool-capture-") as h:
        home = Path(h)
        source = args.source_home.resolve()
        for name in ["config.toml", "auth.json"]:
            shutil.copy2(source / name, home / name)
            os.chmod(home / name, 0o600)
        (home / "plugins").symlink_to(source / "plugins", target_is_directory=True)
        env = os.environ.copy()
        env["CODEX_HOME"] = h
        cmd = [
            str(args.codex.resolve()),
            "-c",
            'model_provider="tool_capture"',
            "-c",
            "features.codex_hooks=false",
            "-c",
            "features.code_mode_host=false",
            "-c",
            f'model_providers.tool_capture={{name="capture",base_url="http://127.0.0.1:{server.server_port}/v1",wire_api="responses",requires_openai_auth=false,request_max_retries=0}}',
            "app-server",
        ]
        with tempfile.TemporaryFile(mode="w+") as err:
            p = subprocess.Popen(
                cmd,
                env=env,
                cwd=h,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                bufsize=1,
            )
            try:
                c = JsonRpcClient(p)
                response_result(
                    c.request(
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "codex_desktop",
                                "version": "0.153.4",
                            },
                            "capabilities": {"experimentalApi": True},
                        },
                        20,
                    ),
                    "initialize",
                )
                c.notify("initialized")
                r = response_result(
                    c.request(
                        "thread/start",
                        {
                            "cwd": h,
                            "model": "xai/grok-4.6",
                            "modelProvider": "tool_capture",
                            "approvalPolicy": "never",
                            "sandbox": "read-only",
                            "ephemeral": False,
                        },
                        45,
                    ),
                    "start",
                )
                t = r["thread"]["id"]
                response_result(
                    c.request(
                        "turn/start",
                        {
                            "threadId": t,
                            "input": [
                                {
                                    "type": "text",
                                    "text": "Reply OK without calling any tools.",
                                }
                            ],
                        },
                        30,
                    ),
                    "turn",
                )
                if not captured.wait(45):
                    raise RuntimeError("capture timeout")
            finally:
                stop_issue106_app_server(p)
    server.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
