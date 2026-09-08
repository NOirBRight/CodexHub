#!/usr/bin/env python3
"""Opt-in real Codex + candidate Gateway + third-party Collaboration E2E.

Uses explicitly selected existing local subscription sessions in a private
throwaway home. Reports structural evidence only; never retains credentials,
conversation bodies, or encrypted content. Ordinary pytest never invokes it.
"""
from __future__ import annotations

from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))
DEFAULT_CHILD = "xai/grok-4.6"
SENTINELS = ("E2E_CHILD_OK 323", "E2E_FOLLOWUP_OK 667")
PROMPT = (
    "This is a real cross-provider collaboration E2E. Explicitly spawn one "
    "subagent with model {child_model}, reasoning_effort {child_effort}, fork_turns none, "
    "task_name grok_probe. Its task is to calculate 17*19 and reply E2E_CHILD_OK "
    "323. Wait for its actual result. Then use followup_task on the same agent "
    "to calculate 23*29 and reply E2E_FOLLOWUP_OK 667. Wait for that result too. "
    "Do not use shell or fake its responses. Report both results only after "
    "receiving them. If any operation errors, report failure and stop."
)


def collect_evidence(home: Path, child_model: str) -> dict:
    child_results: set[str] = set()
    parent_results: set[str] = set()
    plaintext_handoffs = 0
    encrypted_handoffs = 0
    portable_calls: set[str] = set()
    child_models: set[str] = set()
    for path in (home / "sessions").rglob("*.jsonl"):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        models = {row["payload"].get("model") for row in rows if row.get("type") == "turn_context"}
        is_child = child_model in models
        if is_child:
            child_models.add(child_model)
        for row in rows:
            item = row.get("payload", {})
            if row.get("type") != "response_item":
                continue
            if item.get("type") == "function_call" and item.get("namespace") == "collaboration":
                if item.get("encrypted_function_args") == []:
                    portable_calls.add(item.get("name"))
            if is_child and item.get("type") == "agent_message":
                parts = item.get("content", [])
                encrypted_handoffs += sum(part.get("type") == "encrypted_content" for part in parts)
                plaintext_handoffs += sum(part.get("type") == "input_text" for part in parts)
            if item.get("type") == "message" and item.get("role") == "assistant":
                text = "\n".join(part.get("text", "") for part in item.get("content", []))
                for sentinel in SENTINELS:
                    if sentinel in text:
                        (child_results if is_child else parent_results).add(sentinel)
    return {
        "child_models": sorted(child_models),
        "child_completed_results": sorted(child_results),
        "parent_received_results": sorted(parent_results),
        "plaintext_child_handoffs": plaintext_handoffs,
        "encrypted_child_handoffs": encrypted_handoffs,
        "portable_calls": sorted(portable_calls),
        "passed": (
            child_results == set(SENTINELS) and parent_results == set(SENTINELS)
            and plaintext_handoffs >= 2 and encrypted_handoffs == 0
            and {"spawn_agent", "followup_task"} <= portable_calls
        ),
    }


def collect_failure_signals(path: Path) -> list[dict]:
    """Retain only structural error codes and known diagnostic categories."""
    signals = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
            if event.get("type") != "error":
                continue
            message = event.get("message", "")
            payload = json.loads(message)
        except (ValueError, TypeError):
            continue
        error = payload.get("codexhub_error", {})
        details = error.get("details", {})
        signal = {key: error[key] for key in ("code", "source") if key in error}
        signal.update({key: details[key] for key in (
            "status", "reason", "classification", "failure_class", "type",
        ) if key in details})
        signal["categories"] = [name for name in (
            "not supported", "does not exist", "model_not_found", "missing_catalog_model",
            "encrypted_agent_message_unavailable", "configured schema", "tool_choice",
        ) if name in message.lower()]
        if signal not in signals:
            signals.append(signal)
    return signals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--parent-model", default="gpt-6-astra")
    parser.add_argument("--parent-effort", default="low")
    parser.add_argument("--parent-collaboration-version", choices=("v1", "v2"))
    parser.add_argument("--refresh-official", action="store_true")
    parser.add_argument("--child-model", default=DEFAULT_CHILD)
    parser.add_argument("--child-effort", default="high")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--output", type=Path, default=Path("test-results/third-party-collaboration.json"))
    args = parser.parse_args()
    source = args.source_home.expanduser().resolve()
    required = (
        "auth.json", "models_cache.json", "proxy/xai_auth.json",
        "proxy/settings.json", "proxy/official-editor-catalog.json", "proxy/config/providers.toml",
        "model-catalogs/codexhub-model-catalog.json",
    )
    if not all((source / name).is_file() for name in required):
        parser.error("source home lacks required Official/xAI session or catalog inputs")
    report = {"parent_model": args.parent_model, "child_model": args.child_model, "passed": False}
    if args.parent_collaboration_version:
        report["parent_collaboration_version_override"] = args.parent_collaboration_version
    with tempfile.TemporaryDirectory(prefix="codexhub-grok-e2e-") as directory:
        work = Path(directory)
        server_home, client_home = work / "server", work / "client"
        for home in (server_home, client_home):
            home.mkdir()
            shutil.copy2(source / "auth.json", home / "auth.json")
        for name in required[1:]:
            target = server_home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        catalog = client_home / "catalog.json"
        shutil.copy2(source / "model-catalogs/codexhub-model-catalog.json", catalog)
        # Read the candidate's bundled defaults through the production provider
        # loader. A copied live catalog may still select a retired V1 runtime.
        from providers_config import build_external_model_index, load_providers
        candidates = build_external_model_index(
            load_providers(source / "proxy/config/providers.toml"), require_api_key=False,
        )
        catalog_payload = json.loads(catalog.read_text())
        if args.refresh_official:
            refreshed = subprocess.run(
                [sys.executable, str(ROOT / "src-python/official_catalog.py"),
                 "--client-version", "0.153.4", "--timeout", "20"],
                env=dict(os.environ, CODEX_HOME=str(server_home)),
                capture_output=True, text=True, timeout=30, check=True,
            )
            fresh_models = json.loads(refreshed.stdout)["models"]
            by_slug = {model["slug"]: model for model in catalog_payload["models"]}
            for model in fresh_models:
                by_slug[model["slug"]] = model
            catalog_payload["models"] = list(by_slug.values())
            (server_home / "model-catalogs/codexhub-model-catalog.json").write_text(json.dumps(catalog_payload))
            report["refreshed_official_models"] = len(fresh_models)
        for model in catalog_payload["models"]:
            candidate = candidates.get(model.get("slug"), {})
            version = candidate.get("multi_agent_version")
            if version in {"v1", "v2"}:
                model["multi_agent_version"] = version
            if model.get("slug") == args.parent_model and args.parent_collaboration_version:
                model["multi_agent_version"] = args.parent_collaboration_version
        catalog.write_text(json.dumps(catalog_payload))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        key = secrets.token_hex(32)
        server_env = dict(os.environ, CODEX_HOME=str(server_home), CODEX_PROXY_GATEWAY_CLIENT_KEY=key)
        client_env = dict(os.environ, CODEX_HOME=str(client_home), CODEXHUB_E2E_GATEWAY_KEY=key)
        (client_home / "config.toml").write_text(
            f'model_provider = "custom"\nmodel = {json.dumps(args.parent_model)}\n'
            f'model_reasoning_effort = {json.dumps(args.parent_effort)}\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\n'
            f'model_catalog_json = {json.dumps(str(catalog))}\n'
            '[model_providers.custom]\nname = "candidate gateway"\n'
            f'base_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\n'
            'env_key = "CODEXHUB_E2E_GATEWAY_KEY"\nsupports_websockets = false\n'
            '[features]\nmulti_agent = true\napps = false\nplugins = false\n'
            'responses_websockets = false\nresponses_websockets_v2 = false\n'
        )
        with (work / "gateway.log").open("w") as log:
            server = subprocess.Popen(
                [sys.executable, str(ROOT / "src-python/codex_proxy.py"), "--port", str(port)],
                env=server_env, stdout=log, stderr=log, cwd=ROOT,
            )
            try:
                for _ in range(100):
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("candidate_gateway_unavailable")
                with (work / "client.jsonl").open("w") as output:
                    result = subprocess.run(
                        ["codex", "exec", "--json", "--skip-git-repo-check", "-C", str(work), PROMPT.format(child_model=args.child_model, child_effort=args.child_effort)],
                        env=client_env, stdout=output, stderr=subprocess.STDOUT, timeout=args.timeout,
                    )
                report.update(collect_evidence(client_home, args.child_model))
                report["client_exit_code"] = result.returncode
                report["passed"] = report["passed"] and result.returncode == 0
                if not report["passed"]:
                    report["failure_signals"] = collect_failure_signals(work / "client.jsonl")
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                report["failure_type"] = type(error).__name__
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
