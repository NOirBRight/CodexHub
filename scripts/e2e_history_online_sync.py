from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


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


def parse_key_values(stdout: str) -> dict[str, str]:
    return {
        key: value.strip()
        for line in stdout.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def run_history_helper(repo_root: Path, codex_home: Path, backup_root: Path) -> dict[str, str]:
    command = [
        sys.executable,
        str(repo_root / "src-python" / "history_overlay.py"),
        "migrate-official-to-unified",
        "--codex-dir",
        str(codex_home),
        "--backup-root",
        str(backup_root),
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "history helper failed\n"
            f"command: {command}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return parse_key_values(completed.stdout)


def start_app_server(codex_command: Path, codex_home: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [
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
        ],
        cwd=codex_home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"App CLI app-server exited during startup: {stderr}")
        time.sleep(0.05)
    return process


def stop_app_server(process: subprocess.Popen[bytes]) -> None:
    if process.stdin:
        process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stderr:
        process.stderr.close()


def seed_history(codex_home: Path) -> tuple[Path, Path, str]:
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    session_file = sessions / "rollout-online-e2e.jsonl"
    tail = json.dumps({"type": "event_msg", "payload": {"message": "preserve-tail"}}) + "\n"
    session_file.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "thread-online-e2e", "model_provider": "openai"},
            }
        )
        + "\n"
        + tail,
        encoding="utf-8",
    )

    state_path = codex_home / "state_5.sqlite"
    connection = sqlite3.connect(state_path)
    try:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, rollout_path TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES ('thread-online-e2e', 'openai', ?)",
            (str(session_file),),
        )
        connection.commit()
    finally:
        connection.close()
    return state_path, session_file, tail


def write_running_config(codex_home: Path, sqlite_home: Path) -> None:
    (codex_home / "config.toml").write_text(
        f'sqlite_home = "{sqlite_home.as_posix()}"\n',
        encoding="utf-8",
    )


def write_unified_config(codex_home: Path, sqlite_home: Path) -> None:
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                f'sqlite_home = "{sqlite_home.as_posix()}"',
                'model_provider = "custom"',
                "",
                "[model_providers.custom]",
                'name = "OpenAI"',
                'wire_api = "responses"',
                "requires_openai_auth = true",
                "",
            ]
        ),
        encoding="utf-8",
    )


def run(repo_root: Path, codex_command: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="codexhub-history-online-e2e-") as temp_dir:
        codex_home = Path(temp_dir) / ".codex"
        sqlite_home = Path(temp_dir) / "app-server-sqlite"
        codex_home.mkdir()
        sqlite_home.mkdir()
        state_path, session_file, expected_tail = seed_history(codex_home)
        write_running_config(codex_home, sqlite_home)

        app_server = start_app_server(codex_command, codex_home)
        try:
            write_unified_config(codex_home, sqlite_home)
            backup_root = codex_home / "proxy" / "online-e2e"
            writer = sqlite3.connect(state_path)
            writer.execute("BEGIN IMMEDIATE")
            try:
                deferred = run_history_helper(repo_root, codex_home, backup_root)
            finally:
                writer.rollback()
                writer.close()

            if deferred.get("status") != "deferred":
                raise AssertionError(
                    f"expected deferred while SQLite writer lock is held, got {deferred}"
                )
            if deferred.get("reason") != "sqlite_busy":
                raise AssertionError(f"expected sqlite_busy reason, got {deferred}")
            if app_server.poll() is not None:
                raise AssertionError("app-server exited during online history migration")

            completed = run_history_helper(repo_root, codex_home, backup_root)
            if completed.get("status") != "completed":
                raise AssertionError(
                    f"expected completed after releasing SQLite writer lock, got {completed}"
                )
            if app_server.poll() is not None:
                raise AssertionError("app-server exited during online history migration")

            connection = sqlite3.connect(state_path)
            try:
                provider = connection.execute(
                    "SELECT model_provider FROM threads WHERE id = 'thread-online-e2e'"
                ).fetchone()[0]
            finally:
                connection.close()
            lines = session_file.read_text(encoding="utf-8").splitlines(keepends=True)
            session_meta = json.loads(lines[0])
            if provider != "custom" or session_meta["payload"]["model_provider"] != "custom":
                raise AssertionError("SQLite and JSONL providers did not converge to custom")
            if "".join(lines[1:]) != expected_tail:
                raise AssertionError("JSONL tail changed during online migration")
            ledger = json.loads((backup_root / "ledger.json").read_text(encoding="utf-8"))
            if ledger.get("status") != "completed" or "completed_at" not in ledger:
                raise AssertionError(f"completion ledger is invalid: {ledger}")

            return {
                "app_cli": str(codex_command),
                "app_server_pid": app_server.pid,
                "deferred_reason": deferred.get("reason"),
                "final_status": completed.get("status"),
                "provider": provider,
                "tail_preserved": True,
            }
        finally:
            stop_app_server(app_server)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run isolated online history migration while the App-managed Codex CLI app-server stays alive."
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--codex-command")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    result = run(repo_root, resolve_app_codex(args.codex_command))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
