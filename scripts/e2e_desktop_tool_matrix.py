"""Live full-catalog tool-call roundtrips through an isolated Gateway.

The caller supplies a captured Desktop tool catalog and a credential source
home explicitly. Only the synthetic read tool is executed; every other tool
is present for schema acceptance. Temporary credential copies are removed.
"""

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import sys
import os
import json
import tempfile
import shutil
import socket
import subprocess
import time
import secrets
import urllib.request
import urllib.error
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = args.source_home.resolve()
    tools = json.loads(args.tools.read_text())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    alias_count = sum(
        len(t.get("tools", [])) for t in tools if t.get("type") == "namespace"
    ) + sum(t.get("type") == "custom" for t in tools)
    function_count = alias_count + sum(t.get("type") == "function" for t in tools)
    assert tools and alias_count and function_count

    results = []
    with tempfile.TemporaryDirectory(prefix="codexhub-desktop-matrix-") as h:
        home = Path(h)
        for rel in [
            "auth.json",
            "proxy/xai_auth.json",
            "proxy/settings.json",
            "proxy/config/providers.toml",
            "model-catalogs/codexhub-model-catalog.json",
            "proxy/official-editor-catalog.json",
        ]:
            src = source / rel
            if src.exists():
                dst = home / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
        # Keep release state and production control-plane files out of this home.
        key = secrets.token_hex(24)
        env = os.environ.copy()
        env["CODEX_HOME"] = h
        env["CODEX_PROXY_GATEWAY_CLIENT_KEY"] = key
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with tempfile.TemporaryFile(mode="w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(root / "src-python/codex_proxy.py"),
                    "--port",
                    str(port),
                ],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)

                def request(payload):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/responses",
                        json.dumps(payload).encode(),
                        {
                            "Authorization": "Bearer " + key,
                            "Content-Type": "application/json",
                            "User-Agent": "codex-app",
                            "originator": "codex_desktop",
                            "session_id": session_id,
                        },
                    )
                    events = []
                    try:
                        with urllib.request.urlopen(req, timeout=120) as response:
                            for line in response:
                                if line.startswith(b"data: "):
                                    data = line[6:].strip()
                                    if data != b"[DONE]":
                                        events.append(json.loads(data))
                    except urllib.error.HTTPError as e:
                        detail = e.read().decode()
                        raise RuntimeError(f"HTTP {e.code}: {detail[:600]}")
                    failures = [
                        e
                        for e in events
                        if e.get("type")
                        in ["error", "response.failed", "response.incomplete"]
                    ]
                    if failures:
                        raise RuntimeError("SSE failure: " + json.dumps(failures)[:600])
                    completed = [
                        e["response"]
                        for e in events
                        if e.get("type") == "response.completed"
                    ]
                    if len(completed) != 1:
                        raise RuntimeError("missing/duplicate completed response")
                    output = completed[0].get("output", [])
                    if not output:
                        output = [
                            e["item"]
                            for e in events
                            if e.get("type") == "response.output_item.done"
                        ]
                    return output

                for model in [
                    "gpt-5.6-luna",
                    "xai/grok-4.6",
                    "opencode-go/muse-spark-1.3-contributor",
                ]:
                    session_id = secrets.token_hex(16)
                    result = {
                        "model": model,
                        "catalog_aliases": alias_count,
                        "catalog_functions": function_count,
                    }
                    start = time.monotonic()
                    try:
                        sentinel = "DESKTOP_TOOL_MATRIX_" + secrets.token_hex(8)
                        f = home / "probe.txt"
                        f.write_text(sentinel)
                        probe = {
                            "type": "function",
                            "name": "codexhub_e2e_read",
                            "description": "Read the isolated verification fixture. Call with no arguments.",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        }
                        prompt = {
                            "role": "user",
                            "content": "Compatibility verification: call codexhub_e2e_read exactly once with {}. Then reply with the exact text returned by that tool. Do not call any other tools.",
                        }
                        payload = {
                            "model": model,
                            "instructions": "Follow the verification request. Only codexhub_e2e_read may be called.",
                            "input": [prompt],
                            "tools": tools + [probe],
                            "tool_choice": "auto",
                            "stream": True,
                            "store": False,
                        }
                        out = request(payload)
                        calls = [o for o in out if o.get("type") == "function_call"]
                        if (
                            len(calls) != 1
                            or calls[0].get("name") != "codexhub_e2e_read"
                        ):
                            raise RuntimeError(
                                "expected exactly the isolated read tool: "
                                + str([(c.get("name"), c.get("type")) for c in calls])
                            )
                        c = calls[0]
                        if json.loads(c.get("arguments", "{}")) != {}:
                            raise RuntimeError("unexpected read arguments")
                        payload["input"] = [
                            prompt,
                            {
                                "type": "function_call",
                                "name": c["name"],
                                "arguments": c["arguments"],
                                "call_id": c["call_id"],
                            },
                            {
                                "type": "function_call_output",
                                "call_id": c["call_id"],
                                "output": f.read_text(),
                            },
                        ]
                        final = request(payload)
                        text = "".join(
                            c.get("text", "")
                            for o in final
                            for c in o.get("content", [])
                            if c.get("type") == "output_text"
                        )
                        if sentinel not in text or any(
                            o.get("type") == "function_call" for o in final
                        ):
                            raise RuntimeError("read result missing from final answer")
                        result.update(
                            outcome="passed", tool_calls=1, result_roundtrip=True
                        )
                    except Exception as error:
                        result.update(outcome="failed", error=str(error))
                    result["seconds"] = round(time.monotonic() - start, 2)
                    results.append(result)
                    print(json.dumps(result), flush=True)
                # Retain only sanitized structural event evidence.
                events = home / "proxy/codex-proxy-events.jsonl"
                evidence = []
                if events.exists():
                    for l in events.read_text().splitlines():
                        e = json.loads(l)
                        if e.get("event") in [
                            "runtime_tool_adapter_request",
                            "request_complete",
                        ]:
                            evidence.append(
                                {
                                    k: e[k]
                                    for k in [
                                        "event",
                                        "model",
                                        "status",
                                        "adapted_alias_count",
                                        "upstream_function_tool_count",
                                    ]
                                    if k in e
                                }
                            )
                args.output.write_text(
                    json.dumps(
                        {
                            "schema": "codexhub.desktop-tool-matrix.v1",
                            "catalog_sha256": hashlib.sha256(
                                args.tools.read_bytes()
                            ).hexdigest(),
                            "catalog_functions": function_count,
                            "probe_function_count": 1,
                            "results": results,
                            "events": evidence,
                        },
                        indent=2,
                    )
                    + "\n"
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    if any(r["outcome"] != "passed" for r in results):
        sys.exit(1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
