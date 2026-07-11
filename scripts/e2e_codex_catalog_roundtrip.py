from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


def resolve_app_codex(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Codex command does not exist: {candidate}")

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        candidates = sorted(
            (path for path in root.glob("*/codex.exe") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if candidates:
            return candidates[0]

    fallback = shutil.which("codex.exe") or shutil.which("codex.cmd") or shutil.which("codex")
    if fallback:
        return Path(fallback).resolve()
    raise FileNotFoundError("App-managed Codex CLI was not found under OpenAI/Codex/bin or PATH")


def app_server_command(codex_command: Path) -> list[str]:
    return [
        str(codex_command),
        "app-server",
        "--disable",
        "plugins",
        "--disable",
        "remote_plugin",
        "--disable",
        "plugin_sharing",
        "--listen",
        "stdio://",
    ]


def request_model_list(codex_command: Path, codex_home: Path | None) -> list[dict[str, Any]]:
    env = os.environ.copy()
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        app_server_command(codex_command),
        cwd=codex_home or Path.home(),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        assert process.stdin is not None
        requests = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codexhub-catalog-e2e",
                        "title": "CodexHub Catalog E2E",
                        "version": "0.1.4",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                        "optOutNotificationMethods": [],
                    },
                },
            },
            {"method": "initialized"},
            {"id": 2, "method": "model/list", "params": {"limit": 100}},
        ]
        for request in requests:
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None and lines.empty():
                break
            try:
                line = lines.get(timeout=min(0.5, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != 2:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex app-server model/list failed: {message['error']}")
            result = message.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                raise RuntimeError(f"Codex app-server model/list returned an invalid payload: {message}")
            return [item for item in data if isinstance(item, dict)]

        stderr = process.stderr.read() if process.stderr and process.poll() is not None else ""
        raise TimeoutError(f"Codex app-server model/list timed out or exited: {stderr.strip()}")
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        reader.join(timeout=1)


def canonical_official_id(value: object) -> str:
    model_id = str(value or "").strip()
    if model_id.startswith("openai/gpt-"):
        return model_id.removeprefix("openai/")
    return model_id


def short_official_name(value: object, model_id: str) -> str:
    display_name = str(value or model_id).strip()
    if display_name.lower().startswith("openai "):
        display_name = display_name[7:].strip()
    if display_name.lower().startswith("gpt-"):
        display_name = display_name[4:]
    return re.sub(r"[-_]+", " ", display_name).strip()


def official_seed_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in models:
        model_id = canonical_official_id(item.get("model") or item.get("id"))
        if not model_id.startswith("gpt-") or model_id in seen or item.get("hidden") is True:
            continue
        seen.add(model_id)
        model = dict(item)
        model["id"] = model_id
        model["model"] = model_id
        model["slug"] = model_id
        model["display_name"] = short_official_name(item.get("displayName"), model_id)
        efforts = item.get("supportedReasoningEfforts")
        if isinstance(efforts, list):
            model["supported_reasoning_levels"] = [
                {
                    "effort": effort.get("reasoningEffort"),
                    "description": effort.get("description"),
                }
                for effort in efforts
                if isinstance(effort, dict) and effort.get("reasoningEffort")
            ]
        default_effort = item.get("defaultReasoningEffort")
        if isinstance(default_effort, str) and default_effort:
            model["default_reasoning_level"] = default_effort
        input_modalities = item.get("inputModalities")
        if isinstance(input_modalities, list):
            model["input_modalities"] = input_modalities
        model["visibility"] = "list"
        model["supported_in_api"] = True
        model["enabled"] = True
        output.append(model)
    return output


def run_checked(command: list[str], repo_root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def effort_ids(model: dict[str, Any]) -> set[str]:
    raw = model.get("supportedReasoningEfforts") or model.get("supported_reasoning_levels") or []
    return {
        str(item.get("reasoningEffort") or item.get("effort"))
        for item in raw
        if isinstance(item, dict) and (item.get("reasoningEffort") or item.get("effort"))
    }


def run(repo_root: Path, codex_command: Path) -> dict[str, Any]:
    live_models = request_model_list(codex_command, None)
    seeds = official_seed_models(live_models)
    source_ids = [str(model["slug"]) for model in seeds]
    if not source_ids:
        raise AssertionError("live App CLI catalog did not return visible official GPT models")

    with tempfile.TemporaryDirectory(prefix="codexhub-catalog-roundtrip-e2e-") as temp_dir:
        codex_home = Path(temp_dir) / ".codex"
        model_catalog_dir = codex_home / "model-catalogs"
        proxy_dir = codex_home / "proxy"
        model_catalog_dir.mkdir(parents=True)
        proxy_dir.mkdir(parents=True)
        source_codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        source_auth = source_codex_home / "auth.json"
        if source_auth.is_file():
            shutil.copyfile(source_auth, codex_home / "auth.json")
        (model_catalog_dir / "openai-plus-ollama-cloud.json").write_text(
            json.dumps({"client_version": "0.1.4", "models": seeds}, indent=2) + "\n",
            encoding="utf-8",
        )
        (proxy_dir / "settings.json").write_text(
            json.dumps(
                {
                    "include_official_models": True,
                    "official_model_sort_order": source_ids,
                    "official_disabled_models": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        env.pop("OLLAMA_API_KEY", None)
        run_checked(
            [sys.executable, str(repo_root / "src-python" / "catalog_sync.py"), "--sync"],
            repo_root,
            env,
        )

        catalog_path = model_catalog_dir / "codexhub-model-catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog_models = [item for item in catalog.get("models", []) if isinstance(item, dict)]
        catalog_official = [
            item
            for item in catalog_models
            if item.get("codex_proxy_metadata", {}).get("upstream_name") == "official"
        ]
        catalog_ids = [str(item.get("slug")) for item in catalog_official]
        if catalog_ids != source_ids:
            raise AssertionError(
                f"official model order changed during catalog generation: {source_ids} -> {catalog_ids}"
            )
        if len(catalog_ids) != len(set(catalog_ids)):
            raise AssertionError(f"official models were duplicated during catalog generation: {catalog_ids}")
        if any(model_id.startswith("openai/") for model_id in catalog_ids):
            raise AssertionError("custom catalog exposed a prefixed official model id")
        for model in catalog_official:
            label = str(model.get("display_name") or "")
            if label.lower().startswith(("openai ", "gpt-")):
                raise AssertionError(f"custom catalog exposed a prefixed official display label: {label}")

        config_path = codex_home / "config.toml"
        run_checked(
            [
                sys.executable,
                str(repo_root / "src-python" / "config_overlay.py"),
                "apply",
                "--config",
                str(config_path),
                "--backup",
                str(proxy_dir / "config.toml.beta.backup"),
                "--catalog",
                str(catalog_path),
                "--base-url",
                "http://127.0.0.1:65534",
                "--owner",
                "beta",
                "--gateway-key",
                "catalog-e2e-key",
            ],
            repo_root,
            env,
        )
        overlay_text = config_path.read_text(encoding="utf-8")
        if any(line.strip().startswith("model =") for line in overlay_text.splitlines()):
            raise AssertionError("CodexHub overlay must not force a model preference")
        roundtrip_models = request_model_list(codex_command, codex_home)
        roundtrip_all_by_id = {
            str(item.get("model") or item.get("id")): item for item in roundtrip_models
        }
        roundtrip_by_id = {
            canonical_official_id(item.get("model") or item.get("id")): item
            for item in roundtrip_models
            if canonical_official_id(item.get("model") or item.get("id")) in set(source_ids)
        }
        roundtrip_ids = [
            canonical_official_id(item.get("model") or item.get("id"))
            for item in roundtrip_models
            if canonical_official_id(item.get("model") or item.get("id")) in set(source_ids)
        ]
        if roundtrip_ids != source_ids:
            all_roundtrip_ids = [
                str(item.get("model") or item.get("id")) for item in roundtrip_models
            ]
            raise AssertionError(
                "official model order changed after custom catalog roundtrip: "
                f"{source_ids} -> {roundtrip_ids}; all={all_roundtrip_ids}"
            )
        if len(roundtrip_ids) != len(set(roundtrip_ids)):
            raise AssertionError(f"official models were duplicated after App CLI roundtrip: {roundtrip_ids}")
        if any(model_id.startswith("openai/") for model_id in roundtrip_ids):
            raise AssertionError("custom catalog exposed a prefixed official model id")

        sol = roundtrip_by_id.get("gpt-5.6-sol")
        terra = roundtrip_by_id.get("gpt-5.6-terra")
        luna = roundtrip_by_id.get("gpt-5.6-luna")
        standard_efforts = {"low", "medium", "high", "xhigh", "max"}
        third_party = roundtrip_all_by_id.get("minimax-m3")
        if (
            not sol
            or not terra
            or not luna
            or not standard_efforts.issubset(effort_ids(sol))
            or not standard_efforts.issubset(effort_ids(terra))
            or not standard_efforts.issubset(effort_ids(luna))
            or "ultra" not in effort_ids(sol)
            or "ultra" not in effort_ids(terra)
            or "ultra" in effort_ids(luna)
            or not third_party
            or effort_ids(third_party) != standard_efforts
        ):
            raise AssertionError(
                "reasoning contract must preserve Light through Max for every model and Ultra only for Sol/Terra"
            )

        return {
            "app_cli": str(codex_command),
            "official_ids": source_ids,
            "roundtrip_ids": roundtrip_ids,
            "labels": [roundtrip_by_id[model_id].get("displayName") for model_id in source_ids],
            "sol_efforts": sorted(effort_ids(sol)),
            "terra_efforts": sorted(effort_ids(terra)),
            "luna_efforts": sorted(effort_ids(luna)),
            "third_party_efforts": sorted(effort_ids(third_party)),
            "duplicates": False,
            "prefixed_ids": False,
            "isolated_codex_home": True,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Round-trip the live App-managed Codex model catalog through an isolated CodexHub custom provider."
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--codex-command")
    args = parser.parse_args(argv)

    result = run(args.repo_root.resolve(), resolve_app_codex(args.codex_command))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
