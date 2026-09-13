"""Bounded real-Codex discovery E2E with an isolated synthetic MCP server.

Default upstreams are deterministic Responses and Chat fixtures. --live-xai
uses only the explicitly supplied xAI session file. No production Gateway or
client configuration is modified. Output contains counters and verdicts only.
"""
from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src-python")]
from catalog import CatalogPolicy
from catalog_sync import build_external_provider_model
import gateway_catalog_runtime
import gateway_settings
from tests.gateway_harness import GatewayHarness, GATEWAY_CLIENT_KEY
from tool_compatibility.contracts import TOOL_SEARCH_INPUT_KEY

STATUS = "TURQUOISE_DELIVERED"


def mcp_fixture():
    if Path(os.environ.get("CODEXHUB_E2E_PYTHON", "")).resolve() != Path(sys.executable).resolve():
        raise RuntimeError("fixture_python_binding_missing")
    tools = [{"name": f"probe_{i}", "description": f"Synthetic probe {i}",
              "annotations": {"readOnlyHint": True, "openWorldHint": False},
              "inputSchema": {"type": "object", "properties": {}}} for i in range(40)]
    tools[0].update(name="lookup_probe", description="Look up the synthetic turquoise parcel status.")
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "probe", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            called = request.get("params", {}).get("name") == "lookup_probe"
            if called:
                Path(os.environ["CODEXHUB_DISCOVERY_MARKER"]).write_text(STATUS)
            result = {"content": [{"type": "text", "text": STATUS if called else "UNRELATED_PROBE"}]}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


def fixture_response(body, step, chat):
    flat = [t.get("function", t) for t in body.get("tools", [])]
    if step == 1:
        assert not any("turquoise" in t.get("description", "") for t in flat), "tools_not_deferred"
        tool = next(t for t in flat if t.get("name", "").startswith("__codexhub_search_"))
        name = tool["name"]
        arguments = json.dumps({TOOL_SEARCH_INPUT_KEY: {"query": "turquoise parcel lookup_probe", "limit": 1}})
    elif step == 2:
        tool = next(t for t in flat if "turquoise" in t.get("description", ""))
        name, arguments = tool["name"], "{}"
    if chat:
        delta = ({"tool_calls": [{"index": 0, "id": f"call_{step}", "type": "function",
                                  "function": {"name": name, "arguments": arguments}}]}
                 if step < 3 else {"content": STATUS})
        events = [{"id": f"resp_{step}", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": f"resp_{step}", "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls" if step < 3 else "stop"}]}]
        return sse(events) + b"data: [DONE]\n\n"
    if step < 3:
        item = {"id": f"item_{step}", "type": "function_call", "name": name, "arguments": arguments,
                "call_id": f"call_{step}", "status": "completed"}
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"], "delta": arguments},
            {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": item["id"], "arguments": arguments},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
    else:
        item = {"id": "msg_done", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": STATUS}]}
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": [], "status": "in_progress"}},
            {"type": "response.output_text.delta", "item_id": "msg_done", "output_index": 0, "content_index": 0, "delta": STATUS},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
        ]
    events.insert(0, {"type": "response.created", "response": {"id": f"resp_{step}", "status": "in_progress", "output": []}})
    events.append({"type": "response.completed", "response": {"id": f"resp_{step}", "status": "completed", "output": [item]}})
    return sse(events)


def sse(events):
    return b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)


def live_response(body, auth_path):
    auth = json.loads(auth_path.read_text())
    if auth.get("expires_at", 0) < time.time() + 60:
        raise RuntimeError("xai_session_expired")
    request = Request("https://api.x.ai/v1/responses", data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + auth["tokens"]["access_token"], "Content-Type": "application/json"})
    chunks, size, started = [], 0, time.monotonic()
    with urlopen(request, timeout=45) as response:
        while line := response.readline(1024 * 1024):
            chunks.append(line)
            size += len(line)
            if size > 2 * 1024 * 1024 or time.monotonic() - started > 50:
                raise RuntimeError("response_bound_exceeded")
            if line.startswith(b"data: {"):
                event = json.loads(line[5:])
                if event.get("type") in {"response.completed", "response.failed", "response.incomplete", "error"}:
                    break
    return b"".join(chunks) + b"\n"


def run_case(args, slug):
    requests, failures = [], []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw)
                requests.append(body)
                if len(requests) > 5:
                    raise RuntimeError("request_bound_exceeded")
                raw = (live_response(body, args.xai_auth_file) if args.live_xai
                       else fixture_response(body, len(requests), self.path.endswith("/chat/completions")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
            except Exception as error:
                failures.append(type(error).__name__)
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"discovery_fixture_failed"}}')

    with GatewayHarness() as harness, tempfile.TemporaryDirectory(prefix="codexhub-discovery-") as h:
        harness.stub.server.RequestHandlerClass = Provider
        choose = gateway_catalog_runtime.choose_upstream.side_effect
        gateway_catalog_runtime.choose_upstream.side_effect = lambda m: {**choose(m), "tool_surface_strategy": args.strategy}
        home, provider = Path(h), slug.split("/")[0]
        model = build_external_provider_model({"alias": slug, "provider_alias": provider,
            "upstream_name": provider, "upstream_model": slug.split("/")[1], "upstream_format": "responses"},
            CatalogPolicy(set(), set(), {}), None)
        (home / "catalog.json").write_text(json.dumps({"models": [model]}))
        (home / "config.toml").write_text(f'''web_search="disabled"
model_provider="capture"
model={json.dumps(slug)}
model_catalog_json={json.dumps(str(home / "catalog.json"))}
[model_providers.capture]
name="capture"
base_url="http://127.0.0.1:{harness.port}/v1"
wire_api="responses"
env_key="CODEXHUB_DISCOVERY_KEY"
request_max_retries=0
[features]
plugins=false
apps=false
[mcp_servers.probe]
command={json.dumps(sys.executable)}
args=[{json.dumps(str(Path(__file__).resolve()))},"--mcp-fixture"]
[mcp_servers.probe.env]
CODEXHUB_E2E_PYTHON={json.dumps(sys.executable)}
CODEXHUB_DISCOVERY_MARKER={json.dumps(str(home / "tool-executed"))}
''')
        marker = home / "tool-executed"
        env = dict(os.environ, CODEX_HOME=h, CODEXHUB_DISCOVERY_KEY=GATEWAY_CLIENT_KEY,
                   CODEXHUB_E2E_PYTHON=sys.executable, CODEXHUB_DISCOVERY_MARKER=str(marker))
        for key in ("CODEX_APP_TOOLS_PIPE_PATH", "CODEX_THREAD_ID"):
            env.pop(key, None)
        with patch.object(gateway_settings, "transport_sse_idle_timeout_seconds", return_value=55.0):
            result = subprocess.run([str(args.codex.resolve()), "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                "Use tool search to discover the synthetic turquoise parcel lookup tool, call it, and report the exact status it returns. Do not call shell or read files."],
                env=env, cwd=home, stdin=subprocess.DEVNULL, capture_output=True, timeout=170 if args.live_xai else 40)
        observed = any(STATUS in json.dumps(r.get("input", r.get("messages", []))) for r in requests)
        summary = {"model": slug, "strategy": args.strategy, "live": args.live_xai, "exit": result.returncode,
            "requests": len(requests), "tool_counts": [len(r.get("tools", [])) for r in requests],
            "tool_executed": marker.exists(), "tool_result_observed": observed, "failures": failures,
            "completed": STATUS in result.stdout.decode(errors="replace")}
        summary["passed"] = (not failures and result.returncode == 0 and summary["completed"]
                             and summary["tool_executed"] and observed and 3 <= len(requests) <= 5)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-fixture", action="store_true")
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strategy", choices=("eager", "deferred_core"), default="eager")
    parser.add_argument("--live-xai", action="store_true")
    parser.add_argument("--xai-auth-file", type=Path)
    args = parser.parse_args()
    if args.mcp_fixture:
        mcp_fixture()
        return 0
    if not args.codex or not args.output or (args.live_xai and not args.xai_auth_file):
        parser.error("--codex and --output required; --live-xai requires --xai-auth-file")
    summaries = []
    for slug in (["xai/grok-4.6"] if args.live_xai else ["xai/grok-4.6", "volc/glm-5.2"]):
        summary = run_case(args, slug)
        summaries.append(summary)
        print(json.dumps(summary), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summaries, indent=2) + "\n")
    return 0 if all(s["passed"] for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
