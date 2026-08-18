"""Linux real-client preflight for Codex Desktop and ZCode GUI."""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DESKTOP_FLOOR = (26, 715, 8383)
ZCODE_FLOOR = (3, 3, 6)
LAUNCH_WAIT_SECONDS = 12
LAUNCH_KILL_SECONDS = 8


def parse_version(raw: str) -> tuple[int, ...]:
    digits: list[int] = []
    current = ""
    for char in raw:
        if char.isdigit():
            current += char
        elif current:
            digits.append(int(current))
            current = ""
    if current:
        digits.append(int(current))
    return tuple(digits)


def version_at_least(found: tuple[int, ...], floor: tuple[int, ...]) -> bool:
    padded_found = found + (0,) * max(0, len(floor) - len(found))
    padded_floor = floor + (0,) * max(0, len(found) - len(floor))
    return padded_found >= padded_floor


def dpkg_version(package: str) -> str | None:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", package],
        check=False,
        capture_output=True,
        text=True,
    )
    value = (result.stdout or "").strip()
    return value or None


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def detect_desktop() -> dict[str, object]:
    env_path = os.environ.get("CODEXHUB_CODEX_DESKTOP")
    candidates = [Path(env_path)] if env_path else []
    candidates.extend(
        [Path("/usr/bin/chatgpt"), Path("/usr/lib/chatgpt/codex-launcher")]
    )
    exe = first_existing(candidates)
    version = dpkg_version("chatgpt")
    parsed = parse_version(version or "")
    return {
        "client": "codex-desktop",
        "product_name": "Codex Desktop",
        "package": "chatgpt",
        "executable": str(exe) if exe else None,
        "version": version,
        "meets_floor": bool(version) and version_at_least(parsed, DESKTOP_FLOOR),
        "floor": ".".join(str(part) for part in DESKTOP_FLOOR),
    }


def detect_zcode() -> dict[str, object]:
    env_path = os.environ.get("CODEXHUB_ZCODE_EXE")
    candidates = [Path(env_path)] if env_path else []
    candidates.extend([Path("/opt/ZCode/zcode"), Path("/usr/bin/zcode")])
    exe = first_existing(candidates)
    version = dpkg_version("zcode")
    parsed = parse_version(version or "")
    return {
        "client": "zcode",
        "product_name": "ZCode",
        "package": "zcode",
        "executable": str(exe) if exe else None,
        "version": version,
        "meets_floor": bool(version) and version_at_least(parsed, ZCODE_FLOOR),
        "floor": ".".join(str(part) for part in ZCODE_FLOOR),
    }


def write_isolated_fixtures(root: Path) -> tuple[Path, Path]:
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
                "gateway_client_key": "linux-e2e-key",
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
                'id = "volc"',
                'name = "Volcengine"',
                'base_url = "https://example.invalid/v1"',
                'api_key = "linux-e2e-upstream"',
                'display_prefix = "Volc"',
                'upstream_format = "responses"',
                "enabled = true",
                "",
                "  [[providers.models]]",
                '  id = "glm-5.2"',
                '  display_name = "Volc GLM-5.2"',
                "  enabled = true",
                "  gateway_exported = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return settings, providers


def run_managed(
    bin_path: Path, verb: str, client: str, root: Path, settings: Path, providers: Path
) -> dict[str, object]:
    command = [
        str(bin_path),
        "managed-client-config",
        verb,
        "--client",
        client,
        "--root",
        str(root),
        "--model",
        "volc/glm-5.2",
        "--settings-path",
        str(settings),
        "--providers-path",
        str(providers),
        "--python-path",
        sys.executable,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    try:
        payload: dict[str, object] = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"raw": (result.stdout or "")[:500]}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stderr": (result.stderr or "").strip()[:800],
        "value": payload,
    }


def isolated_apply(bin_path: Path, client: str, work: Path) -> dict[str, object]:
    fixtures = work / "fixtures"
    apply_root = work / f"{client}-apply"
    fixtures.mkdir(parents=True, exist_ok=True)
    apply_root.mkdir(parents=True, exist_ok=True)
    settings, providers = write_isolated_fixtures(fixtures)
    apply = run_managed(bin_path, "apply", client, apply_root, settings, providers)
    readback = (
        run_managed(bin_path, "readback", client, apply_root, settings, providers)
        if apply["ok"]
        else {"ok": False, "returncode": None, "stderr": "skipped", "value": {}}
    )
    return {"apply": apply, "readback": readback}


def launch_isolated(executable: str, name: str, work: Path) -> dict[str, object]:
    home = work / f"{name}-home"
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / "config")
    env["XDG_CACHE_HOME"] = str(home / "cache")
    env["XDG_DATA_HOME"] = str(home / "data")
    env["CODEX_HOME"] = str(home / ".codex")
    env.pop("ELECTRON_RUN_AS_NODE", None)
    command = [executable]
    if name == "desktop":
        command.extend(["--user-data-dir", str(home / "config" / "Codex")])
    if name == "zcode":
        command.extend(["--user-data-dir", str(home / "config" / "ZCode")])
    process = subprocess.Popen(
        command,
        cwd=str(home),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    started = time.time()
    try:
        while time.time() - started < LAUNCH_WAIT_SECONDS:
            code = process.poll()
            if code is None:
                return {"ok": True, "pid": process.pid, "home": str(home)}
            return {
                "ok": False,
                "pid": process.pid,
                "error": f"process exited immediately with {code}",
            }
        return {"ok": False, "pid": process.pid, "error": "process did not stay running"}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            deadline = time.time() + LAUNCH_KILL_SECONDS
            while process.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Linux Codex Desktop + ZCode GUI E2E preflight")
    parser.add_argument(
        "--bin",
        default=str(
            Path(__file__).resolve().parents[1] / "src-tauri" / "target" / "debug" / "codexhub"
        ),
    )
    parser.add_argument("--output", default="test-results/linux-gui-e2e.json")
    parser.add_argument("--skip-apply", action="store_true")
    parser.add_argument("--skip-launch", action="store_true")
    parser.add_argument("--detect-only", action="store_true")
    args = parser.parse_args(argv)

    report: dict[str, object] = {
        "schema": "codexhub.linux-gui-e2e.v1",
        "verification_scope": "linux_gui_preflight",
        "desktop": detect_desktop(),
        "zcode": detect_zcode(),
    }
    failures: list[str] = []
    for key in ("desktop", "zcode"):
        client = report[key]
        if not isinstance(client, dict):
            continue
        if not client.get("executable"):
            failures.append(f"{key}: executable not found")
        elif not client.get("meets_floor"):
            failures.append(
                f"{key}: version {client.get('version')} below floor {client.get('floor')}"
            )

    if not args.detect_only and not args.skip_apply:
        bin_path = Path(args.bin)
        if not bin_path.is_file():
            failures.append(f"codexhub binary missing: {bin_path}")
        else:
            with tempfile.TemporaryDirectory(prefix="codexhub-linux-gui-e2e-") as temp:
                work = Path(temp)
                report["codex_apply"] = isolated_apply(bin_path, "codex", work)
                report["zcode_apply"] = isolated_apply(bin_path, "zcode", work)
                if not report["codex_apply"]["apply"]["ok"] or not report["codex_apply"]["readback"]["ok"]:
                    failures.append("codex isolated apply/readback failed")
                if not report["zcode_apply"]["apply"]["ok"] or not report["zcode_apply"]["readback"]["ok"]:
                    failures.append("zcode isolated apply/readback failed")
                if not args.skip_launch:
                    desktop_exe = report["desktop"].get("executable") if isinstance(report["desktop"], dict) else None
                    zcode_exe = report["zcode"].get("executable") if isinstance(report["zcode"], dict) else None
                    if desktop_exe:
                        report["desktop_launch"] = launch_isolated(str(desktop_exe), "desktop", work)
                        if not report["desktop_launch"]["ok"]:
                            failures.append(f"desktop launch: {report['desktop_launch'].get('error')}")
                    if zcode_exe:
                        report["zcode_launch"] = launch_isolated(str(zcode_exe), "zcode", work)
                        if not report["zcode_launch"]["ok"]:
                            failures.append(f"zcode launch: {report['zcode_launch'].get('error')}")

    report["failures"] = failures
    report["ok"] = not failures
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Report: {output}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
