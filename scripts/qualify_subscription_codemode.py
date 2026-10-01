"""Bounded actual Codex Code Mode/V2 probes against an isolated production Gateway.

No substitute tool codec, task repair, bridge executable, or tool executor is
installed by this harness. The observer forwards the original HTTP/SSE bytes.
Raw CLI/rollout data and copied accounts live only in a private temporary tree;
only structural proofs, exact fixture outputs, hashes and bounded errors escape.
"""
from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import gzip
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
CASES = ("code-mode", "official-to-cursor", "cursor-to-official", "completed-history-restart")
CURSOR_MODEL = "cursor-subscription/gpt-5.6-luna-high"
OFFICIAL_MODEL = "gpt-6-astra"
MAX_CAPTURE = 64 * 1024 * 1024


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode()).hexdigest()


def freeze_candidate(checkout, destination):
    """Freeze production files before any request; restart loads identical bytes."""
    aggregate = hashlib.sha256()
    for name in ("src-python", "config", "model-catalogs"):
        source = checkout / name
        if source.is_dir():
            shutil.copytree(source, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif name == "model-catalogs":
            (destination / name).mkdir(parents=True)
        else:
            raise ValueError("candidate-runtime-missing")
        for path in sorted((destination / name).rglob("*")):
            if path.is_file():
                aggregate.update(str(path.relative_to(destination)).encode() + b"\0")
                aggregate.update(hashlib.sha256(path.read_bytes()).digest())
    return aggregate.hexdigest()


def declaration_summary(tools):
    """Public structural observer; never retain dynamic tool descriptions."""
    out = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            out.extend(dict(row, namespace=tool.get("name")) for row in declaration_summary(tool.get("tools")))
        else:
            name = tool.get("name")
            if not isinstance(name, str):
                function = tool.get("function", {})
                name = function.get("name") if isinstance(function, dict) else None
            out.append({"type": tool.get("type"), "name": name,
                        "schema_sha256": digest(json.dumps(tool.get("parameters", tool.get("format", {})), sort_keys=True))})
    return out


def observe_request(body, epoch, nonce):
    """Read-only request evidence; does not return an adapted payload."""
    items = body.get("input", [])
    if not isinstance(items, list):
        items = []
    tools = list(body.get("tools", [])) if isinstance(body.get("tools"), list) else []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools.extend(item.get("tools", []) if isinstance(item.get("tools"), list) else [])
    outputs = [{"type": item["type"], "call_sha256": digest(item.get("call_id", "")),
                "item_sha256": digest(item.get("id", "")) if item.get("id") else None,
                "fixture_present": nonce in json.dumps(item, ensure_ascii=False),
                "reversed_fixture_present": nonce[::-1] in json.dumps(item, ensure_ascii=False)}
               for item in items if isinstance(item, dict) and item.get("type") in ("custom_tool_call_output", "function_call_output")]
    reasoning = body.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else body.get("reasoning_effort")
    effort = effort if effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra") else "absent" if effort is None else "unsupported"
    parallel = body.get("parallel_tool_calls")
    parallel = parallel if isinstance(parallel, bool) else "absent" if parallel is None else "invalid"
    return {"epoch": epoch, "model": body.get("model"), "requested_reasoning_effort": effort, "parallel_tool_calls": parallel, "input_types": [item.get("type") for item in items if isinstance(item, dict)],
            "input_sha256": digest(json.dumps(items, sort_keys=True, ensure_ascii=False)),
            "declarations": declaration_summary(tools), "tool_outputs": outputs,
            "agent_messages": [{"author_sha256": digest(item.get("author", "")), "recipient_sha256": digest(item.get("recipient", "")),
                                "part_types": [part.get("type") for part in item.get("content", []) if isinstance(part, dict)]}
                               for item in items if isinstance(item, dict) and item.get("type") == "agent_message"]}


def summarize_rollouts(paths, nonce):
    """Prove actual caller effects/child identity from private Codex rollouts."""
    sessions = []
    for path in sorted(paths):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        items = [row.get("payload", {}) for row in rows if row.get("type") == "response_item"]
        meta = next((row.get("payload", {}) for row in rows if row.get("type") == "session_meta"), {})
        models = sorted({row.get("payload", {}).get("model") for row in rows if isinstance(row.get("payload", {}).get("model"), str)})
        source = meta.get("source")
        parent = source.get("subagent") if isinstance(source, dict) else None
        is_child = bool(parent)
        calls = []
        effects = []
        for item in items:
            kind = item.get("type")
            if kind in ("function_call", "custom_tool_call"):
                raw = item.get("input", item.get("arguments", ""))
                raw = raw if isinstance(raw, str) else json.dumps(raw)
                try:
                    args = json.loads(raw)
                    args = args if isinstance(args, dict) else {}
                except ValueError:
                    args = {}
                target = args.get("target")
                task_name = args.get("task_name")
                canonical_target = (target if target.startswith("/") else "/root/" + target) if isinstance(target, str) else None
                calls.append({"target_sha256": digest(canonical_target) if canonical_target else None,
                              "spawn_target_sha256": digest("/root/" + task_name) if isinstance(task_name, str) else None,
                              "type": kind, "name": item.get("name"), "namespace": item.get("namespace"),
                              "call_sha256": digest(item.get("call_id", "")), "item_sha256": digest(item.get("id", "")) if item.get("id") else None,
                              "reads_fixture": "exec_command" in raw and "probe-value.txt" in raw,
                              "spawn": "spawn_agent" in str(item.get("name", "")) or "spawn_agent" in raw,
                              "followup": "followup_task" in str(item.get("name", "")) or "followup_task" in raw,
                              "wait": "wait_agent" in str(item.get("name", "")) or "wait_agent" in raw})
            elif kind in ("function_call_output", "custom_tool_call_output"):
                data = json.dumps(item, ensure_ascii=False)
                effects.append({"call_sha256": digest(item.get("call_id", "")), "fixture_present": nonce in data,
                                "reversed_fixture_present": nonce[::-1] in data})
        assistant = "\n".join(part.get("text", "") for item in items if item.get("type") == "message" and item.get("role") == "assistant"
                              for part in item.get("content", []) if isinstance(part, dict))
        sessions.append({"session_sha256": digest(meta.get("id", path.name)), "is_child": is_child, "models": models,
                         "calls": calls, "effects": effects, "assistant_returned_fixture": nonce in assistant,
                         "assistant_returned_reversed_fixture": nonce[::-1] in assistant, "rollout_sha256": digest(path.read_bytes())})
    return sessions


def assess_case(case, turns, requests, sessions, nonce, gateway_pids):
    """Public qualification oracle: protocol success alone never passes a case."""
    good = bool(turns) and all(row["exit"] == 0 and not row["timed_out"] and not row["errors"] for row in turns)
    parent = [session for session in sessions if not session["is_child"]]
    children = [session for session in sessions if session["is_child"]]
    custom = any(call["type"] == "custom_tool_call" and call["name"] in ("exec", "functions.exec") and call["reads_fixture"] for session in sessions for call in session["calls"])
    read_calls = {call["call_sha256"] for session in sessions for call in session["calls"] if call["reads_fixture"] and call["type"] == "custom_tool_call"}
    actual_effect = any(effect["fixture_present"] and effect["call_sha256"] in read_calls for session in sessions for effect in session["effects"])
    first_exact = bool(turns) and turns[0]["finals"] == [nonce]
    expected_models = {OFFICIAL_MODEL, CURSOR_MODEL} if case in ("official-to-cursor", "cursor-to-official") else {CURSOR_MODEL}
    actual_models = {row.get("model") for row in requests if isinstance(row.get("model"), str)}
    result = {"exact_selected_model_identity": bool(actual_models) and actual_models.issubset(expected_models), "actual_custom_exec": custom, "actual_fixture_tool_result": actual_effect, "first_exact": first_exact}
    if case in ("official-to-cursor", "cursor-to-official"):
        parent_calls = [call for session in parent for call in session["calls"]]
        child_reads = sum(call["reads_fixture"] for session in children for call in session["calls"])
        spawned = {call.get("spawn_target_sha256") for call in parent_calls if call["spawn"] and call.get("spawn_target_sha256")}
        followups = {call.get("target_sha256") for call in parent_calls if call["followup"] and call.get("target_sha256")}

        result.update(one_child=len(children) == 1, parent_did_not_read=not any(call["reads_fixture"] for call in parent_calls),
                      spawn=any(call["spawn"] for call in parent_calls), same_child_followup=bool(followups) and len(spawned) == 1 and followups == spawned and len(children) == 1,
                      wait=any(call["wait"] for call in parent_calls), child_read_twice=child_reads >= 2,
                      child_both_results=len(children) == 1 and children[0]["assistant_returned_fixture"] and children[0]["assistant_returned_reversed_fixture"],
                      direction_models={OFFICIAL_MODEL, CURSOR_MODEL}.issubset({row.get("model") for row in requests}))
        first_exact = bool(turns) and turns[0]["finals"] == [nonce + "\n" + nonce[::-1]]
        result["first_exact"] = first_exact
    if case == "completed-history-restart" or case in ("official-to-cursor", "cursor-to-official"):
        previous_calls = {out["call_sha256"] for row in requests if row["epoch"] == 0 for out in row["tool_outputs"]}
        replayed = {out["call_sha256"] for row in requests if row["epoch"] == 1 for out in row["tool_outputs"]}
        result.update(actual_gateway_restart=len(gateway_pids) == 2 and gateway_pids[0] != gateway_pids[1],
                      fresh_caller=len(turns) == 2 and turns[0]["pid"] != turns[1]["pid"],
                      completed_calls_replayed=bool(previous_calls) and previous_calls.issubset(replayed),
                      restart_exact=len(turns) == 2 and turns[1]["finals"] == [nonce[::-1]])
    result["passed"] = good and all(result.values())
    return result


def private_environment(home, path):
    env = {key: value for key, value in os.environ.items() if key in {
        "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
        "CODEXHUB_PYTHON", "CODEXHUB_E2E_PYTHON",
    }}
    env.update({"PATH": path, "HOME": str(home), "USERPROFILE": str(home), "CODEX_HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
                "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "localappdata"),
                "TMPDIR": str(home / "tmp"), "TMP": str(home / "tmp"), "TEMP": str(home / "tmp"),
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
    (home / "tmp").mkdir(mode=0o700, exist_ok=True)
    return env


def stop_process(child):
    if child is None:
        return
    if os.name == "nt":
        subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"), "/PID", str(child.pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill() if os.name == "nt" else os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
    if os.name != "nt":
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def parse_turn(stdout, child, timed_out, nonce):
    events = []
    for line in stdout.splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                events.append(row)
        except ValueError:
            continue
    finals = [event.get("item", {}).get("text", "") for event in events if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message"]
    # Only exact controlled fixture outputs are published; wrong finals retain
    # their full bytes privately and an exact hash/length/zero-width count here.
    finals = finals[-1:]
    public_finals = [value if value in (nonce, nonce[::-1], nonce + "\n" + nonce[::-1]) else {"sha256": digest(value), "length": len(value), "zero_width_count": value.count("\u200b")} for value in finals]
    return {"exit": child.returncode, "pid": child.pid, "timed_out": timed_out, "finals": public_finals,
            "errors": [{"type": row.get("type"), "sha256": digest(json.dumps(row, sort_keys=True))} for row in events if row.get("type") in ("error", "turn.failed")]}


def run_case(case, checkout, source_codex, source_user, codex, timeout):
    """Run one disposable production candidate; cleanup includes raw accounts/logs."""
    case_started = time.monotonic()
    from claude_native_models import concrete_executable
    cursor = shutil.which("cursor-agent")
    if not cursor:
        raise ValueError("official-cursor-cli-missing")
    cursor = concrete_executable(Path(cursor), "cursor-agent")
    with tempfile.TemporaryDirectory(prefix="codexhub-codemode-qualification-") as directory:
        runtime = Path(directory)
        runtime.chmod(0o700)
        server, client, fixture = (runtime / name for name in ("server", "client", "fixture"))
        for home in (server, client, fixture):
            home.mkdir(mode=0o700)
        nonce = secrets.token_hex(12)
        (fixture / "probe-value.txt").write_text(nonce + "\n")
        server_env = private_environment(server, str(cursor.parent) + os.pathsep + os.environ.get("PATH", os.defpath))
        client_env = private_environment(client, os.environ.get("PATH", os.defpath))
        for relative in ("auth.json", "proxy/official-editor-catalog.json", "model-catalogs/codexhub-model-catalog.json"):
            source = source_codex / relative
            if source.is_file():
                target = server / relative
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                target.write_bytes(source.read_bytes())
                target.chmod(0o600)
        cursor_config = Path(os.environ.get("APPDATA") or source_user / "AppData/Roaming") if os.name == "nt" else Path(os.environ.get("XDG_CONFIG_HOME") or source_user / ".config")
        cursor_auth = json.loads((cursor_config / ("Cursor" if os.name == "nt" else "cursor") / "auth.json").read_text())
        target = Path(server_env["APPDATA"] if os.name == "nt" else server_env["XDG_CONFIG_HOME"]) / ("Cursor" if os.name == "nt" else "cursor") / "auth.json"
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_text(json.dumps({"accessToken": cursor_auth["accessToken"]}))
        target.chmod(0o600)
        original = json.loads((source_codex / "model-catalogs/codexhub-model-catalog.json").read_text())
        official = next((row for row in original["models"] if row.get("slug") == OFFICIAL_MODEL), None)
        if official is None:
            raise ValueError("official-model-not-in-source-catalog")
        from providers_config import ModelConfig, ProviderConfig, save_providers, build_external_model_index
        from catalog_sync import build_codex_catalog
        from catalog import load_policy
        provider = ProviderConfig(id="cursor-subscription", name="Cursor", base_url="", api_key="", upstream_format="chat_completions", tool_protocol="chat_tools",
                                  models=[ModelConfig(id="gpt-5.6-luna-high", upstream_model="gpt-5.6-luna-high", multi_agent_version="v2", capabilities_edited=True, supported_reasoning_levels=(), default_reasoning_level=None)])
        provider_path = server / "proxy/config/providers.toml"
        provider_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        save_providers([provider], provider_path)
        provider_path.chmod(0o600)
        policy = load_policy(checkout / "config/catalog_policy.toml")
        actual_version = subprocess.run([str(codex), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip().split()[-1]
        catalog = build_codex_catalog([official], [], policy, actual_version, external_models=build_external_model_index([provider]).values())
        (client / "catalog.json").write_text(json.dumps(catalog))
        settings = server / "proxy/settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"gateway_auto_retry_enabled": False, "gateway_auto_retry_max_attempts": 1,
                                        "gateway_official_http_passthrough_enabled": True, "gateway_request_timeout_seconds": timeout,
                                        "gateway_image_proxy_enabled": False}))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            gateway_port = sock.getsockname()[1]
        key = secrets.token_hex(24)
        server_env.update(CODEX_PROXY_GATEWAY_CLIENT_KEY=key, PYTHONPATH=str(checkout / "src-python"))
        client_env["SUBSCRIPTION_QUALIFICATION_KEY"] = key
        requests = []
        epoch = [0]
        class Observer(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args):
                pass
            def do_GET(self):
                self.forward()
            def do_POST(self):
                self.forward()
            def forward(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_CAPTURE:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                row = {"epoch": epoch[0], "path": self.path}
                try:
                    parsed = json.loads(gzip.decompress(body) if self.headers.get("Content-Encoding") == "gzip" else body) if body else {}
                    row.update(observe_request(parsed, epoch[0], nonce))
                except (ValueError, OSError):
                    row["observation_invalid"] = True
                requests.append(row)
                connection = http.client.HTTPConnection("127.0.0.1", gateway_port, timeout=timeout)
                try:
                    # Content-Encoding and original body are deliberately kept.
                    headers = {name: value for name, value in self.headers.items() if name.lower() not in ("host", "connection", "transfer-encoding")}
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    row["status"] = response.status
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in ("connection", "transfer-encoding", "content-length"):
                            self.send_header(name, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    failure_body = bytearray()
                    while data := response.read1(65536):
                        if response.status >= 400 and len(failure_body) < 8192:
                            failure_body.extend(data[:8192 - len(failure_body)])
                        self.wfile.write(data)
                        self.wfile.flush()
                    if response.status >= 400:
                        row["error_body_sha256"] = digest(bytes(failure_body))
                        try:
                            error_payload = json.loads(failure_body)
                            code = error_payload.get("error", {}).get("code")
                            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", code):
                                row["error_code"] = code
                        except (ValueError, AttributeError):
                            pass
                except (OSError, http.client.HTTPException):
                    row["stream_interrupted"] = True
                finally:
                    connection.close()
                    self.close_connection = True
        observer = ThreadingHTTPServer(("127.0.0.1", 0), Observer)
        observer.daemon_threads = True
        observer_thread = threading.Thread(target=observer.serve_forever, daemon=True)
        observer_thread.start()
        parent_model = OFFICIAL_MODEL if case == "official-to-cursor" else CURSOR_MODEL
        child_model = CURSOR_MODEL if case == "official-to-cursor" else OFFICIAL_MODEL
        reasoning_config = 'model_reasoning_effort="high"\n' if parent_model == OFFICIAL_MODEL else ""
        (client / "config.toml").write_text(f'''model_provider="qualification"
model={json.dumps(parent_model)}
{reasoning_config}approval_policy="never"
sandbox_mode="read-only"
web_search="disabled"
model_catalog_json={json.dumps(str(client / 'catalog.json'))}
[model_providers.qualification]
name="isolated production Gateway qualification"
base_url="http://127.0.0.1:{observer.server_port}/v1"
wire_api="responses"
env_key="SUBSCRIPTION_QUALIFICATION_KEY"
requires_openai_auth=false
supports_websockets=false
request_max_retries=0
stream_max_retries=0
[features]
code_mode=true
code_mode_host=true
multi_agent=true
multi_agent_v2=true
apps=false
plugins=false
hooks=false
shell_snapshot=false
memories=false
enable_request_compression=false
unbounded_connection_retries=false
''')
        gateway = None
        gateway_pids = []
        turns = []
        sessions = []
        log = (runtime / "gateway.log").open("w")
        def start_gateway():
            process = subprocess.Popen([sys.executable, str(checkout / "src-python/codex_proxy.py"), "--port", str(gateway_port)],
                                       env=server_env, cwd=checkout, stdout=log, stderr=log, start_new_session=os.name != "nt")
            gateway_pids.append(process.pid)
            deadline = time.monotonic() + 15
            opener = build_opener(ProxyHandler({}))
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stop_process(process)
                    raise ValueError("production-gateway-exited-before-readiness")
                try:
                    with opener.open(f"http://127.0.0.1:{gateway_port}/health", timeout=.25):
                        return process
                except OSError:
                    time.sleep(.1)
            stop_process(process)
            raise ValueError("production-gateway-readiness-timeout")
        prompt = ('Use the actual Code Mode custom exec tool to call tools.exec_command with cmd="cat probe-value.txt", login=false. '
                  'Use text to emit its result. Read only this fixture. Do not modify anything or spawn agents. '
                  'Final response must be exactly the file value, with no markdown and no extra characters.')
        if case in ("official-to-cursor", "cursor-to-official"):
            child_effort = ", reasoning_effort high" if child_model == OFFICIAL_MODEL else ", omit reasoning_effort because its exact selected ID already fixes the vendor variant"
            prompt = ('This is a controlled cross-provider V2 qualification. Spawn exactly one child named reader, '
                      f'with model {child_model}, fork_turns none{child_effort}. Parent must never read the fixture. '
                      'Assign the child this plaintext task: use actual Code Mode exec with tools.exec_command to read probe-value.txt and return its exact contents. '
                      'Keep every assignment plaintext; do not set encrypted_function_args and do not encrypt or label text as encrypted_content. '
                      'Wait for the child, then followup_task to the SAME child: read the same fixture again and return its contents reversed. '
                      'Wait again. Use only collaboration handlers actually declared. Never fabricate results or edit files. '
                      'Final response must be exactly two lines: first original file contents, then reversed contents, with no extra characters.')
        caller = None
        failure = None
        try:
            gateway = start_gateway()
            for turn in range(2 if case != "code-mode" else 1):
                remaining = timeout - (time.monotonic() - case_started)
                if remaining <= 0:
                    raise ValueError("case-budget-exhausted")
                if turn:
                    stop_process(gateway)
                    epoch[0] = 1
                    gateway = start_gateway()
                    command = [str(codex), "exec", "resume", "--last", "--json", "--skip-git-repo-check",
                               "Without reading any file, using any tool, or spawning/reissuing an agent, return the exact original fixture value from our completed previous turn reversed. No extra characters."]
                else:
                    command = [str(codex), "exec", "--json", "--skip-git-repo-check", "--ignore-rules", "-C", str(fixture), prompt]
                remaining = timeout - (time.monotonic() - case_started)
                if remaining <= 0:
                    raise ValueError("case-budget-exhausted")
                capture = runtime / f"caller-{turn}.jsonl"
                with capture.open("wb") as output:
                    caller = subprocess.Popen(command, env=client_env, cwd=fixture, stdout=output, stderr=subprocess.DEVNULL,
                                              start_new_session=os.name != "nt")
                    timed_out = False
                    try:
                        caller.wait(timeout=max(.1, remaining))
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        stop_process(caller)
                with capture.open("rb") as captured:
                    data = captured.read(MAX_CAPTURE + 1)
                if len(data) > MAX_CAPTURE:
                    raise ValueError("caller-capture-size-exceeded")
                turns.append(parse_turn(data.decode("utf-8"), caller, timed_out, nonce))
                if caller.returncode:
                    break
            sessions = summarize_rollouts((client / "sessions").rglob("*.jsonl"), nonce)
        except Exception as error:
            failure = type(error).__name__  # never expose raw CLI/vendor diagnostics
        finally:
            stop_process(caller)
            stop_process(gateway)
            log.close()
            observer.shutdown()
            observer.server_close()
            observer_thread.join(timeout=2)
        checks = assess_case(case, turns, requests, sessions, nonce, gateway_pids)
        if failure:
            checks["passed"] = False
        result = {"case": case, "checks": checks, "turns": turns, "requests": requests, "sessions": sessions,
                "fixture_sha256": digest(nonce), "gateway_pids": gateway_pids, "failure_class": failure,
                "elapsed_seconds": round(time.monotonic() - case_started, 3)}
    result["private_tree_removed"] = not runtime.exists()
    if not result["private_tree_removed"]:
        result["checks"]["passed"] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=ROOT)
    parser.add_argument("--source-codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--source-user-home", type=Path, default=Path.home())
    parser.add_argument("--codex", type=Path, default=Path(shutil.which("codex") or "codex"))
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--case-timeout", type=int, default=180)
    parser.add_argument("--total-timeout", type=int, default=1200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.case_timeout <= 180 or not 1 <= args.total_timeout <= 1200:
        parser.error("case timeout must be <=180s and total timeout <=1200s")
    selected_cases = tuple(args.case or CASES)
    if len(selected_cases) > 4 or len(set(selected_cases)) != len(selected_cases):
        parser.error("select at most four distinct cases; duplicate cases are not allowed")
    sys.path.insert(0, str(args.checkout.resolve() / "src-python"))
    version = subprocess.run([str(args.codex), "--version"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=args.checkout, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    report = {"scope": "isolated actual Codex and production Gateway; passive byte-preserving observer; no injected codec", "candidate_sha": sha,
              "codex_version": version, "case_timeout_seconds": args.case_timeout, "total_timeout_seconds": args.total_timeout,
              "platform": sys.platform, "cases": []}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codexhub-codemode-candidate-") as frozen:
        snapshot = Path(frozen)
        report["runtime_snapshot_sha256"] = freeze_candidate(args.checkout.resolve(), snapshot)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "src-python", "config", "model-catalogs"], cwd=args.checkout, capture_output=True, text=True, timeout=5, check=True).stdout
        report["source_runtime_dirty"] = bool(dirty.strip())
        report["candidate_sha_is_exact_runtime"] = not report["source_runtime_dirty"]
        sys.path.insert(0, str(snapshot / "src-python"))
        for case in selected_cases:
            remaining = args.total_timeout - (time.monotonic() - started)
            if remaining <= 0:
                report["budget_exhausted"] = True
                break
            try:
                report["cases"].append(run_case(case, snapshot, args.source_codex_home, args.source_user_home,
                                                 args.codex.resolve(), min(args.case_timeout, remaining)))
            except Exception as error:
                report["cases"].append({"case": case, "checks": {"passed": False}, "failure_class": type(error).__name__})
    report["snapshot_tree_removed"] = not snapshot.exists()
    report["passed"] = report["snapshot_tree_removed"] and bool(report["cases"]) and all(case["checks"]["passed"] for case in report["cases"]) and not report.get("budget_exhausted")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"passed": report["passed"], "cases": [{"case": case["case"], "checks": case["checks"], "failure_class": case.get("failure_class")} for case in report["cases"]]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
