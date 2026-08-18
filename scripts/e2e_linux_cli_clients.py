"""Four-CLI Linux E2E against a packaged CodexHub binary and the local Gateway."""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SENTINEL = "CODEXHUB_LINUX_CLI_SENTINEL_OK"
PROMPT = (
    "Read ./sentinel.txt exactly once using a read-only file tool. "
    "Reply with only the exact file contents and then stop. Do not modify files."
)


def write_fixtures(root: Path) -> tuple[Path, Path]:
    settings = root / "settings.json"
    providers = root / "providers.toml"
    settings.write_text(
        json.dumps(
            {
                "locale": "",
                "auto_sync_history": False,
                "unified_codex_history": True,
                "auto_start_software": True,
                "auto_start_gateway": True,
                "include_official_models": False,
                "auto_sync_catalog": True,
                "auto_sync_clients": True,
                "default_codex_route": "hub",
                "gateway_bind_address": "127.0.0.1",
                "gateway_client_key": "codexhub-proxy",
                "gateway_enable_models": True,
                "gateway_enable_responses": True,
                "gateway_enable_chat_completions": True,
                "gateway_request_timeout_seconds": 300,
                "gateway_auto_retry_enabled": True,
                "gateway_auto_retry_max_attempts": 30,
                "gateway_image_proxy_enabled": False,
                "gateway_image_proxy_model": "",
                "openai_context_guard_enabled": False,
                "gateway_fast_model_variants": ["gpt-5.5", "gpt-5.4"],
                "official_disabled_models": [],
                "official_model_sort_order": [],
                "official_provider_sort_order": 0,
                "proxy_port": 9099,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    providers.write_text(
        "\n".join(
            [
                "[[providers]]",
                'id = "openai"',
                'name = "OpenAI"',
                'base_url = "https://example.invalid/v1"',
                'api_key = "unused-upstream"',
                'display_prefix = "openai/"',
                'upstream_format = "responses"',
                "enabled = true",
                "",
                "  [[providers.models]]",
                '  id = "gpt-5.6-luna"',
                '  display_name = "GPT-5.6 Luna"',
                "  enabled = true",
                "  gateway_exported = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return settings, providers


def managed(bin_path: Path, verb: str, client: str, root: Path, settings: Path, providers: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            str(bin_path),
            "managed-client-config",
            verb,
            "--client",
            client,
            "--root",
            str(root),
            "--model",
            "openai/gpt-5.6-luna",
            "--settings-path",
            str(settings),
            "--providers-path",
            str(providers),
            "--python-path",
            sys.executable,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"raw": (result.stdout or "")[:400]}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stderr": (result.stderr or "").strip()[:600],
        "value": payload,
    }


def run_cmd(command: list[str], env: dict[str, str], cwd: Path, timeout: int) -> dict[str, object]:
    result = subprocess.run(
        command,
        check=False,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (result.stdout or "") + "\n" + (result.stderr or "")
    return {
        "ok": result.returncode == 0 and SENTINEL in out,
        "returncode": result.returncode,
        "saw_sentinel": SENTINEL in out,
        "output_tail": out[-1200:],
        "command": command,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True)
    parser.add_argument("--output", default="test-results/linux-cli-e2e.json")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)
    bin_path = Path(args.bin)
    report: dict[str, object] = {"schema": "codexhub.linux-cli-e2e.v1", "bin": str(bin_path)}
    failures: list[str] = []
    apply_results: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="codexhub-linux-cli-e2e-") as temp:
        work = Path(temp)
        fixtures = work / "fixtures"
        fixtures.mkdir()
        settings, providers = write_fixtures(fixtures)
        clients = ["codex", "opencode", "pi", "omp"]
        apply_results = {}
        for client in clients:
            root = work / f"{client}-root"
            root.mkdir()
            apply = managed(bin_path, "apply", client, root, settings, providers)
            readback = managed(bin_path, "readback", client, root, settings, providers) if apply["ok"] else {"ok": False}
            apply_results[client] = {"apply": apply, "readback": readback, "root": str(root)}
            if not apply["ok"] or not readback.get("ok"):
                failures.append(f"{client}: packaged apply/readback failed")
        report["apply"] = apply_results
        if not args.skip_live and not failures:
            live = {}
            case = work / "case"
            case.mkdir()
            (case / "sentinel.txt").write_text(SENTINEL + "\n", encoding="utf-8")
            env = os.environ.copy()
            # Codex
            codex_home = Path(apply_results["codex"]["root"]) / "codex-target"
            cenv = env.copy()
            cenv["CODEX_HOME"] = str(codex_home)
            live["codex"] = run_cmd(
                ["codex", "exec", "-C", str(case), "--skip-git-repo-check", "--sandbox", "read-only", "-m", "openai/gpt-5.6-luna", PROMPT],
                cenv,
                case,
                args.timeout,
            )
            # OpenCode
            ohome = work / "opencode-home"
            ocfg = ohome / ".config" / "opencode"
            ocfg.mkdir(parents=True)
            src = Path(apply_results["opencode"]["root"]) / "opencode" / "opencode.json"
            if src.exists():
                shutil.copy2(src, ocfg / "opencode.json")
            oenv = env.copy()
            oenv["HOME"] = str(ohome)
            oenv["XDG_CONFIG_HOME"] = str(ohome / ".config")
            live["opencode"] = run_cmd(
                ["opencode", "run", "--dir", str(case), "--pure", "--title", "linux-cli-e2e", "--model", "codexhub-openai/gpt-5.6-luna", "--auto", PROMPT],
                oenv,
                case,
                args.timeout,
            )
            # Pi
            phome = work / "pi-home"
            pagent = phome / ".pi" / "agent"
            pagent.mkdir(parents=True)
            psrc = Path(apply_results["pi"]["root"]) / "pi" / "models.json"
            if psrc.exists():
                shutil.copy2(psrc, pagent / "models.json")
            penv = env.copy()
            penv["HOME"] = str(phome)
            penv["PI_CODING_AGENT_DIR"] = str(pagent)
            live["pi"] = run_cmd(
                ["pi", "-p", "--no-session", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", "--tools", "read", "--model", "gpt-5.6-luna", PROMPT],
                penv,
                case,
                args.timeout,
            )
            # OMP
            mhome = work / "omp-home"
            magent = mhome / ".omp" / "agent"
            magent.mkdir(parents=True)
            oroot = Path(apply_results["omp"]["root"]) / "omp"
            for name in ("config.yml", "models.yml"):
                srcp = oroot / name
                if srcp.exists():
                    shutil.copy2(srcp, magent / name)
            menv = env.copy()
            menv["HOME"] = str(mhome)
            live["omp"] = run_cmd(
                ["omp", "--cwd", str(case), "--print", "--no-session", "--tools", "read", "--auto-approve", PROMPT],
                menv,
                case,
                args.timeout,
            )
            report["live"] = live
            for name, result in live.items():
                if not result.get("ok"):
                    failures.append(f"{name}: live sentinel failed")
    report["failures"] = failures
    report["ok"] = not failures
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": report["ok"],
        "failures": failures,
        "apply": {k: {"apply": v["apply"]["ok"], "readback": v["readback"].get("ok")} for k, v in apply_results.items()},
        "live": {k: {"ok": v.get("ok"), "saw_sentinel": v.get("saw_sentinel"), "returncode": v.get("returncode")} for k, v in (report.get("live") or {}).items()},
    }, indent=2))
    print(f"Report: {out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
