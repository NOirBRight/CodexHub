"""Build and run the eight-case Linux real-client CLI E2E matrix."""
# ruff: noqa: E402

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NamedTuple
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_PREFIX = "SENTINEL:codexhub-linux-cli-e2e:"
PROMPT_TEMPLATE = (
    "Use exactly one read-only tool call to read ./sentinel.txt. "
    "Then reply with only this exact line and no other text: {sentinel}"
)


class Case(NamedTuple):
    case_id: str
    client: str
    provider: str
    managed_model: str
    selector: str
    gateway_model: str


CASES = tuple(
    Case(
        f"{client}-{provider}",
        client,
        provider,
        managed_model,
        selector,
        gateway_model,
    )
    for client, selectors in {
        "codex": (
            ("openai", "gpt-5.6-luna", "gpt-5.6-luna", "gpt-5.6-luna"),
            (
                "opencode-go",
                "opencode-go/muse-spark-1.2-contributor",
                "opencode-go/muse-spark-1.2-contributor",
                "opencode-go/muse-spark-1.2-contributor",
            ),
        ),
        "opencode": (
            (
                "openai",
                "openai/gpt-5.6-luna",
                "codexhub-openai/gpt-5.6-luna",
                "gpt-5.6-luna",
            ),
            (
                "opencode-go",
                "opencode-go/muse-spark-1.2-contributor",
                "codexhub-opencode-go/muse-spark-1.2-contributor",
                "opencode-go/muse-spark-1.2-contributor",
            ),
        ),
        "pi": (
            (
                "openai",
                "openai/gpt-5.6-luna",
                "codexhub-openai/gpt-5.6-luna",
                "gpt-5.6-luna",
            ),
            (
                "opencode-go",
                "opencode-go/muse-spark-1.2-contributor",
                "codexhub-opencode-go/muse-spark-1.2-contributor",
                "opencode-go/muse-spark-1.2-contributor",
            ),
        ),
        "omp": (
            (
                "openai",
                "openai/gpt-5.6-luna",
                "codexhub-openai/gpt-5.6-luna",
                "gpt-5.6-luna",
            ),
            (
                "opencode-go",
                "opencode-go/muse-spark-1.2-contributor",
                "codexhub-opencode-go/muse-spark-1.2-contributor",
                "opencode-go/muse-spark-1.2-contributor",
            ),
        ),
    }.items()
    for provider, managed_model, selector, gateway_model in selectors
)


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 180,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def build_candidate() -> Path:
    env = os.environ.copy()
    env["CODEXHUB_BUILD_FLAVOR"] = "debug"
    result = _run(
        ["cargo", "build", "--locked", "--features", "debug-diagnostics"],
        cwd=ROOT / "src-tauri",
        env=env,
        timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cargo build failed: {(result.stderr or '')[-2000:]}")
    binary = ROOT / "src-tauri" / "target" / "debug" / "codexhub"
    if not binary.is_file():
        raise RuntimeError(f"self-built candidate missing: {binary}")
    return binary


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(port: int, timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def _prepare_runtime(
    work: Path,
    settings_source: Path,
    providers_source: Path,
    auth_source: Path,
    catalog_source: Path | None,
) -> tuple[dict[str, str], Path, Path, Path, int]:
    runtime = work / "runtime"
    codex_home = work / "codex-home"
    proxy = runtime / "proxy"
    config = proxy / "config"
    config.mkdir(parents=True)
    codex_home.mkdir()
    port = _free_port()
    settings = json.loads(settings_source.read_text(encoding="utf-8"))
    settings.update(
        {
            "auto_start_gateway": False,
            "gateway_bind_address": "127.0.0.1",
            "gateway_client_key": secrets.token_hex(32),
            "gateway_enable_models": True,
            "gateway_enable_responses": True,
            "gateway_enable_chat_completions": True,
            "include_official_models": True,
            "proxy_port": port,
        }
    )
    settings_path = proxy / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    providers_path = config / "providers.toml"
    shutil.copy2(providers_source, providers_path)
    shutil.copy2(auth_source, codex_home / "auth.json")
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(work / "home"),
            "XDG_CONFIG_HOME": str(work / "home" / ".config"),
            "XDG_DATA_HOME": str(work / "home" / ".local" / "share"),
            "CODEX_HOME": str(codex_home),
            "CODEXHUB_RUNTIME_HOME": str(runtime),
            "CODEXHUB_ROLLBACK_PROVENANCE_DIR": str(work / "rollback"),
            "CODEXHUB_PYTHON": sys.executable,
            "CODEXHUB_PROXY_PYTHON": sys.executable,
        }
    )
    Path(env["HOME"]).mkdir(parents=True)
    catalog = runtime / "model-catalogs" / "codexhub-model-catalog.json"
    if catalog_source and catalog_source.is_file():
        catalog.parent.mkdir(parents=True)
        shutil.copy2(catalog_source, catalog)
    return env, settings_path, providers_path, catalog, port


def _managed(
    binary: Path,
    verb: str,
    case: Case,
    root: Path,
    settings: Path,
    providers: Path,
    catalog: Path,
    env: dict[str, str],
) -> dict[str, object]:
    command = [
        str(binary),
        "managed-client-config",
        verb,
        "--client",
        case.client,
        "--root",
        str(root),
        "--model",
        case.managed_model,
        "--settings-path",
        str(settings),
        "--providers-path",
        str(providers),
        "--python-path",
        sys.executable,
    ]
    if case.provider == "openai":
        command += ["--catalog-path", str(catalog)]
    result = _run(command, env=env)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stderr": (result.stderr or "")[-600:],
        "value": payload,
    }


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def _client_launch(
    case: Case,
    managed_root: Path,
    case_root: Path,
    base_env: dict[str, str],
    timeout: int,
) -> dict[str, object]:
    sentinel = SENTINEL_PREFIX + case.case_id
    (case_root / "sentinel.txt").write_text(sentinel + "\n", encoding="utf-8")
    prompt = PROMPT_TEMPLATE.format(sentinel=sentinel)
    env = base_env.copy()
    home = case_root / "home"
    home.mkdir()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    if case.client == "codex":
        env["CODEX_HOME"] = str(managed_root / "codex-target")
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(case_root),
            "-m",
            case.selector,
            "-s",
            "read-only",
            "-c",
            "features.apps=false",
            "-",
        ]
        input_text = prompt
    elif case.client == "opencode":
        _copy_tree(managed_root / "opencode", Path(env["XDG_CONFIG_HOME"]) / "opencode")
        command = [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            case.selector,
            "--dir",
            str(case_root),
            "--title",
            "codexhub-linux-cli-e2e",
            "--pure",
            "--auto",
            prompt,
        ]
        input_text = None
    elif case.client == "pi":
        agent = home / ".pi" / "agent"
        _copy_tree(managed_root / "pi", agent)
        env["PI_CODING_AGENT_DIR"] = str(agent)
        command = [
            "pi",
            "--print",
            "--mode",
            "json",
            "--model",
            case.selector,
            "--no-session",
            "--tools",
            "read",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            prompt,
        ]
        input_text = None
    else:
        _copy_tree(managed_root / "omp", home / ".omp" / "agent")
        command = [
            "omp",
            "--print",
            "--mode",
            "json",
            "--model",
            case.selector,
            "--no-session",
            "--no-title",
            "--tools",
            "read",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--cwd",
            str(case_root),
            prompt,
        ]
        input_text = None
    try:
        result = _run(
            command, env=env, cwd=case_root, timeout=timeout, input_text=input_text
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return {
            "ok": result.returncode == 0 and sentinel in output,
            "returncode": result.returncode,
            "saw_sentinel": sentinel in output,
            "output_tail": output[-1600:],
        }
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode(errors="replace")
            if isinstance(error.stdout, bytes)
            else error.stdout or ""
        )
        stderr = (
            error.stderr.decode(errors="replace")
            if isinstance(error.stderr, bytes)
            else error.stderr or ""
        )
        return {
            "ok": False,
            "timed_out": True,
            "output_tail": (stdout + "\n" + stderr)[-1600:],
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bin", type=Path, help="skip self-build and use this candidate"
    )
    parser.add_argument(
        "--output", type=Path, default=Path("test-results/linux-cli-e2e.json")
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--auth", type=Path, default=Path.home() / ".codex" / "auth.json"
    )
    parser.add_argument(
        "--providers",
        type=Path,
        default=Path.home() / ".codex" / "proxy" / "config" / "providers.toml",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path.home() / ".codex" / "proxy" / "settings.json",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path.home()
        / ".codex"
        / "model-catalogs"
        / "codexhub-model-catalog.json",
        help="reuse a current Official catalog without restarting a running Codex Desktop",
    )
    args = parser.parse_args(argv)
    missing = [
        str(path)
        for path in (args.auth, args.providers, args.settings)
        if not path.is_file()
    ]
    clients = [
        name for name in ("codex", "opencode", "pi", "omp") if not shutil.which(name)
    ]
    if missing or clients:
        parser.error(f"missing inputs={missing}; missing clients={clients}")
    binary = args.bin.resolve() if args.bin else build_candidate()
    report: dict[str, object] = {
        "schema": "codexhub.linux-cli-e2e.v2",
        "candidate": str(binary),
        "cases": [],
    }
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="codexhub-linux-cli-e2e-") as temporary:
        work = Path(temporary)
        env, settings, providers, catalog, port = _prepare_runtime(
            work, args.settings, args.providers, args.auth, args.catalog
        )
        refresh = (
            None
            if catalog.is_file()
            else _run([str(binary), "refresh-models"], env=env, timeout=180)
        )
        if not catalog.is_file():
            failures.append("candidate: refresh-models failed")
            report["bootstrap_tail"] = (
                ((refresh.stdout or "") + "\n" + (refresh.stderr or ""))[-1600:]
                if refresh
                else "Official catalog is missing"
            )
        else:
            start = _run([str(binary), "start"], env=env, timeout=30)
            if start.returncode != 0 or not _wait_for_health(port):
                failures.append("candidate: Gateway failed to become healthy")
                report["bootstrap_tail"] = (
                    (start.stdout or "") + "\n" + (start.stderr or "")
                )[-1600:]
            else:
                try:
                    for case in CASES:
                        managed_root = work / "managed" / case.case_id
                        managed_root.mkdir(parents=True)
                        apply = _managed(
                            binary,
                            "apply",
                            case,
                            managed_root,
                            settings,
                            providers,
                            catalog,
                            env,
                        )
                        readback = (
                            _managed(
                                binary,
                                "readback",
                                case,
                                managed_root,
                                settings,
                                providers,
                                catalog,
                                env,
                            )
                            if apply["ok"]
                            else {"ok": False}
                        )
                        result: dict[str, object] = {
                            "case_id": case.case_id,
                            "client": case.client,
                            "provider": case.provider,
                            "selector": case.selector,
                            "managed_model": case.managed_model,
                            "gateway_model": case.gateway_model,
                            "apply": apply,
                            "readback": readback,
                        }
                        if apply["ok"] and readback["ok"]:
                            case_root = work / "cases" / case.case_id
                            case_root.mkdir(parents=True)
                            result["live"] = _client_launch(
                                case, managed_root, case_root, env, args.timeout
                            )
                        else:
                            result["live"] = {"ok": False, "not_run": True}
                        if not result["live"]["ok"]:
                            failures.append(f"{case.case_id}: live sentinel failed")
                        report["cases"].append(result)
                finally:
                    _run([str(binary), "stop"], env=env, timeout=30)
    report["failures"] = failures
    report["ok"] = not failures
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "failures": failures,
                "cases": [
                    {
                        "case_id": item["case_id"],
                        "apply": item["apply"]["ok"],
                        "readback": item["readback"]["ok"],
                        "live": item["live"]["ok"],
                    }
                    for item in report["cases"]
                ],
            },
            indent=2,
        )
    )
    print(f"Report: {args.output}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
