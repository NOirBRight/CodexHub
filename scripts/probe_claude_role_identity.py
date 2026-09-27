"""Capture real Claude CLI family/explicit model IDs on loopback only.

The mock deliberately returns HTTP 400 after capture. This proves client wire
identity, not provider inference or subscription billing/credits behavior.
Run inside bwrap --unshare-net; no real credentials are read.
"""
from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_code_projection import client_environment, projected_model_id, resolve_projected_model_id


def probe(binary: Path) -> list[dict]:
    captured = []

    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path.startswith("/v1/messages"):
                captured.append(body.get("model"))
            data = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                              "message": "Local model identity capture complete"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    rows = []
    try:
        for target in ("gpt-6-astra", "opencode-go/deepseek-v4.1-flash"):
            catalog = {"models": [{"slug": target}]}
            mappings, _ = client_environment(catalog, mappings={"fable": target})
            cases = (
                ("fable", mappings["ANTHROPIC_DEFAULT_FABLE_MODEL"], target),
                (projected_model_id(target), projected_model_id(target), target),
                ("claude-fable-5-1", "claude-fable-5-1", "claude-fable-5-1"),
            )
            for requested, expected, canonical in cases:
                with tempfile.TemporaryDirectory(prefix="codexhub-role-wire-") as directory:
                    root = Path(directory)
                    config = root / ".claude"
                    config.mkdir()
                    (config / "settings.json").write_text(json.dumps({"env": {
                        **mappings, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                        "ANTHROPIC_AUTH_TOKEN": "isolated-loopback-only"}}))
                    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TERM")}
                    env.update(HOME=str(root), CLAUDE_CONFIG_DIR=str(config), TMPDIR=str(root),
                               XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"),
                               XDG_DATA_HOME=str(root / "data"), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                               DISABLE_AUTOUPDATER="1", HTTP_PROXY="http://127.0.0.1:1",
                               HTTPS_PROXY="http://127.0.0.1:1", NO_PROXY="127.0.0.1,localhost")
                    start = len(captured)
                    result = subprocess.run([str(binary), "--bare", "--tools", "", "--strict-mcp-config",
                        "--setting-sources", "user", "--no-session-persistence", "-p",
                        "--model", requested, "Say OK"], cwd=root, env=env,
                        capture_output=True, text=True, timeout=18)
                    wire = captured[start:]
                    passed = (result.returncode == 1 and wire == [expected]
                              and resolve_projected_model_id(expected, catalog) == canonical)
                    rows.append({"target": target, "requested": requested, "wire_models": wire,
                                 "expected_http_status": 400, "cli_exit": result.returncode, "passed": passed})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.claude_bin)
    print(json.dumps({"scope": "client_wire_identity_only", "cases": result}, indent=2))
    raise SystemExit(0 if all(row["passed"] for row in result) else 1)
