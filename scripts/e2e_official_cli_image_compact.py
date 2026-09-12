#!/usr/bin/env python3
"""Isolated Codex CLI official-passthrough check for tool-result images.

Does not touch the production Gateway on :9099 or the original AYASpace session.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zlib
import struct
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))
sys.path.insert(0, str(ROOT / "scripts"))
from python_runtime_contract import require_python_313

require_python_313(__file__)
import e2e_image_tool_compact as e2e

SOURCE = Path.home() / ".codex"
CONTINUE = "E2E_OFFICIAL_CLI_CONTINUE_OK"
MODEL = "gpt-5.6-sol"


def png_magenta(size: int = 64) -> bytes:
    return e2e.png_solid(220, 20, 160, size)


def wait_health(port: int, timeout: float = 20) -> None:
    e2e._wait_health(port, timeout)


def event_names(path: Path) -> list[str]:
    names: list[str] = []
    if not path.is_file():
        return names
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = obj.get("event") or obj.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def find_sessions(home: Path) -> list[Path]:
    sessions = home / "sessions"
    if not sessions.is_dir():
        return []
    return sorted(sessions.rglob("*.jsonl"))


def inspect_rollout(path: Path) -> dict:
    images = 0
    stringified = 0
    function_outputs = 0
    compacted = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = obj.get("type")
        if typ == "compacted":
            compacted += 1
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        if not isinstance(payload, dict):
            continue
        if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            function_outputs += 1
            blob = json.dumps(payload)
            output = payload.get("output")
            if isinstance(output, str) and "data:image" in output:
                stringified += 1
            if isinstance(output, list):
                for part in output:
                    if isinstance(part, dict) and (
                        part.get("type") in {"input_image", "image_url", "image"}
                        or isinstance(part.get("image_url"), (str, dict))
                    ):
                        images += 1
            if isinstance(output, str) and output.strip().startswith("["):
                # json.dumps list as text
                if "input_image" in output or "data:image" in output:
                    stringified += 1
    return {
        "path": str(path),
        "structured_tool_images": images,
        "stringified_tool_images": stringified,
        "function_outputs": function_outputs,
        "compacted": compacted,
    }


def main() -> int:
    report = {
        "report_version": 1,
        "model": MODEL,
        "client": "codex-cli",
        "cli_version": None,
        "passed": False,
        "status": "failed",
    }
    cli = shutil.which("codex")
    if not cli:
        report["status"] = "unverified"
        report["failure_classification"] = "codex_cli_missing"
        print(json.dumps(report, indent=2))
        return 2
    ver = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=20)
    report["cli_version"] = (ver.stdout or ver.stderr or "").strip()
    auth = SOURCE / "auth.json"
    if not auth.is_file():
        report["status"] = "unverified"
        report["failure_classification"] = "missing_chatgpt_auth"
        print(json.dumps(report, indent=2))
        return 2

    with tempfile.TemporaryDirectory(prefix="codexhub-official-cli-e2e-") as directory:
        work = Path(directory)
        server_home = work / "gateway"
        cli_home = work / "cli-home"
        workspace = work / "workspace"
        for path in (server_home, cli_home, workspace):
            path.mkdir(parents=True)
        required = e2e.REQUIRED_SOURCE_FILES
        missing = [name for name in required if not (SOURCE / name).is_file()]
        if missing:
            report["status"] = "unverified"
            report["failure_classification"] = "missing_source_home_inputs"
            report["missing"] = missing
            print(json.dumps(report, indent=2))
            return 2
        for name in required:
            target = server_home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, target)
        shutil.copy2(auth, server_home / "auth.json")
        shutil.copy2(auth, cli_home / "auth.json")
        shot = workspace / "e2e-shot.png"
        shot.write_bytes(png_magenta(64))
        (workspace / "README.txt").write_text("official cli image compact fixture\n", encoding="utf-8")

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        key = secrets.token_hex(32)
        settings_path = server_home / "proxy/settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings.update(
            {
                "auto_start_gateway": False,
                "gateway_bind_address": "127.0.0.1",
                "gateway_client_key": key,
                "include_official_models": True,
                "proxy_port": port,
            }
        )
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        catalog_src = SOURCE / "model-catalogs/codexhub-model-catalog.json"
        if catalog_src.is_file():
            catalog_dst = server_home / "model-catalogs/codexhub-model-catalog.json"
            catalog_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(catalog_src, catalog_dst)
            cli_cat = cli_home / "model-catalogs/codexhub-model-catalog.json"
            cli_cat.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(catalog_src, cli_cat)

        cli_config = f"""model_provider = "custom"
model = "{MODEL}"
model_reasoning_effort = "low"
approval_policy = "never"
sandbox_mode = "workspace-write"
suppress_unstable_features_warning = true
model_catalog_json = '{cli_home / "model-catalogs/codexhub-model-catalog.json"}'
[model_providers.custom]
name = "Codex Proxy"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "{key}"
supports_websockets = false
[features]
view_image = true
remote_compaction_v2 = true
compaction_image_budget = true
"""
        (cli_home / "config.toml").write_text(cli_config, encoding="utf-8")

        env = dict(os.environ)
        env["CODEX_HOME"] = str(server_home)
        env["CODEX_PROXY_GATEWAY_CLIENT_KEY"] = key
        env["PYTHONPATH"] = str(ROOT / "src-python")
        for name in ("CODEXHUB_CODEX_TARGET_HOME", "CODEXHUB_RUNTIME_HOME", "CODEXHUB_HOME", "CODEX_PROXY_HOME"):
            env.pop(name, None)
        # Official ChatGPT often works direct; still honor local US proxy if present.
        proxy = "http://127.0.0.1:7890"
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
        env["https_proxy"] = proxy
        env["http_proxy"] = proxy

        log_path = work / "gateway.log"
        with log_path.open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, str(ROOT / "src-python/codex_proxy.py"), "--host", "127.0.0.1", "--port", str(port)],
                env=env,
                stdout=log,
                stderr=log,
                cwd=str(ROOT),
            )
        try:
            wait_health(port)
            cli_env = dict(os.environ)
            cli_env["CODEX_HOME"] = str(cli_home)
            cli_env["HOME"] = str(work / "fake-home")
            Path(cli_env["HOME"]).mkdir(parents=True, exist_ok=True)
            prompt = (
                "You must call the view_image tool on ./e2e-shot.png before answering. "
                "After viewing, reply with exactly one word: MAGENTA. No other text."
            )
            cmd = [
                cli,
                "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(workspace),
                "-m",
                MODEL,
                "--json",
                prompt,
            ]
            run = subprocess.run(cmd, env=cli_env, cwd=str(workspace), capture_output=True, text=True, timeout=240)
            report["exec_returncode"] = run.returncode
            report["exec_stdout_chars"] = len(run.stdout or "")
            report["exec_stderr_chars"] = len(run.stderr or "")
            combined = (run.stdout or "") + "\n" + (run.stderr or "")
            report["exec_has_magenta"] = "MAGENTA" in combined
            sessions = find_sessions(cli_home)
            report["session_count"] = len(sessions)
            inspections = [inspect_rollout(path) for path in sessions]
            report["sessions"] = inspections
            events_path = server_home / "proxy/codex-proxy-events.jsonl"
            names = event_names(events_path)
            report["event_names"] = sorted(set(names))
            starts = e2e._event_fields(events_path, "request_start")
            adapted = e2e._event_fields(events_path, "tool_result_media_adapted")
            report["request_start_count"] = len(starts)
            report["adapted_event_count"] = len(adapted)
            report["official_upstreams"] = [
                start.get("upstream") for start in starts if isinstance(start, dict)
            ]
            structured = sum(item["structured_tool_images"] for item in inspections)
            stringified = sum(item["stringified_tool_images"] for item in inspections)
            report["structured_tool_images"] = structured
            report["stringified_tool_images"] = stringified

            # Follow-up via CLI resume. Exec-level flags must precede the
            # resume subcommand; --last/--all belong to resume itself.
            if sessions:
                session_id = None
                stem = sessions[-1].stem
                marker = stem.rfind("-01")
                if marker >= 0:
                    session_id = stem[marker + 1 :]
                resume_cmd = [
                    cli,
                    "exec",
                    "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-C",
                    str(workspace),
                    "-m",
                    MODEL,
                    "--json",
                    "resume",
                    "--all",
                ]
                if session_id:
                    resume_cmd.append(session_id)
                else:
                    resume_cmd.append("--last")
                resume_cmd.append(
                    f"Do not call tools. Reply with exactly {CONTINUE} and nothing else."
                )
                resume = subprocess.run(
                    resume_cmd,
                    env=cli_env,
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=240,
                )
                report["resume_returncode"] = resume.returncode
                report["resume_session_id"] = session_id
                report["resume_has_sentinel"] = CONTINUE in ((resume.stdout or "") + (resume.stderr or ""))
                if resume.returncode != 0:
                    report["resume_excerpt"] = ((resume.stderr or resume.stdout or "")[-400:])
                sessions = find_sessions(cli_home)
                inspections = [inspect_rollout(path) for path in sessions]
                report["sessions_after_resume"] = inspections
                names = event_names(events_path)
                report["event_names"] = sorted(set(names))
                adapted = e2e._event_fields(events_path, "tool_result_media_adapted")
                report["adapted_event_count"] = len(adapted)
                structured = sum(item["structured_tool_images"] for item in inspections)
                stringified = sum(item["stringified_tool_images"] for item in inspections)
                report["structured_tool_images"] = structured
                report["stringified_tool_images"] = stringified

            # Compact the same tool-result images through this Gateway as official.
            data_url = e2e.data_url_for_png(shot.read_bytes())
            compact_status, compact_body = e2e._post_gateway(
                port=port,
                key=key,
                payload={"model": MODEL, "stream": True, "input": e2e._compact_history([data_url, data_url])},
                compact=True,
                timeout=180,
            )
            compact_text, _completed = e2e._read_sse_text(compact_body)
            report["compact_http_status"] = compact_status
            report["compact_summary_chars"] = len(compact_text)
            if compact_status >= 400:
                report["compact_error"] = e2e._sanitize_error_excerpt(compact_body)
            names = event_names(events_path)
            report["event_names"] = sorted(set(names))
            starts = e2e._event_fields(events_path, "request_start")
            adapted = e2e._event_fields(events_path, "tool_result_media_adapted")
            report["request_start_count"] = len(starts)
            report["adapted_event_count"] = len(adapted)
            report["official_upstreams"] = [start.get("upstream") for start in starts if isinstance(start, dict)]

            if run.returncode != 0 and structured == 0:
                report["status"] = "unverified"
                report["failure_classification"] = "official_cli_exec_failed"
                excerpt = ((run.stderr or run.stdout or "")[-400:]).replace(key, "REDACTED")
                report["exec_excerpt"] = excerpt
                print(json.dumps(report, indent=2))
                return 2
            if stringified:
                report["failure_classification"] = "tool_result_images_stringified"
                report["status"] = "failed"
                print(json.dumps(report, indent=2))
                return 1
            if adapted:
                report["failure_classification"] = "official_path_ran_third_party_adapter"
                report["status"] = "failed"
                print(json.dumps(report, indent=2))
                return 1
            if structured == 0:
                report["status"] = "unverified"
                report["failure_classification"] = "no_tool_result_image_observed"
                print(json.dumps(report, indent=2))
                return 2
            official_ok = any(u in {"official", "openai"} for u in report.get("official_upstreams") or [])
            if starts and not official_ok:
                report["status"] = "unverified"
                report["failure_classification"] = "not_official_upstream"
                print(json.dumps(report, indent=2))
                return 2
            if not report.get("resume_has_sentinel"):
                report["status"] = "unverified" if report.get("resume_returncode") not in {0, None} else "failed"
                report["failure_classification"] = "official_cli_resume_unverified"
                print(json.dumps(report, indent=2))
                return 2 if report["status"] == "unverified" else 1
            report["passed"] = True
            report["status"] = "passed"
            print(json.dumps(report, indent=2))
            return 0
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
