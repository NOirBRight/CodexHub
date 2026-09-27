"""A login child owned and reaped by the running Web supervisor.

Commands cross a private filesystem mailbox, never an unauthenticated HTTP
endpoint. Only the live child handle is used for cancellation.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import chatgpt_web_runtime as runtime


class LoginSession:
    def __init__(self, home: Path, entry: Path):
        self.home, self.entry = home, entry
        self.child: subprocess.Popen | None = None
        self.request_id: str | None = None
        (home / "login-command.json").unlink(missing_ok=True)
        self._state(False)

    def _state(self, opened: bool, error: str | None = None) -> None:
        runtime._write_json(self.home / "window.json", {
            "open": opened, "error": error, "request_id": self.request_id,
        })

    def close(self) -> None:
        if self.child is not None:
            if self.child.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.child.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                else:
                    os.killpg(self.child.pid, signal.SIGTERM)
                try:
                    self.child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(self.child.pid, signal.SIGKILL)
                    self.child.kill()
                    self.child.wait(timeout=3)
            self.child = None
        self._state(False)

    def tick(self) -> None:
        command = runtime._read_json(self.home / "login-command.json") or {}
        request_id = command.get("request_id")
        if request_id and request_id != self.request_id:
            self.request_id = request_id
            if command.get("action") == "close":
                self.close()
            elif command.get("action") == "open":
                if self.child is None:
                    try:
                        self.child = subprocess.Popen(
                            [str(self.entry), "login"], env=runtime._runtime_env(self.home),
                            cwd=str(runtime._web_home(self.home)), start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                    except OSError:
                        self._state(False, "login_start_failed")
                        return
                self._state(True)
        if self.child is not None and self.child.poll() is not None:
            succeeded = self.child.returncode == 0
            self.child = None
            if succeeded:
                # serve loaded its configuration before this login. The user
                # explicitly restarts it after verification; never auto-restart.
                runtime._write_lifecycle(self.home, enabled=True, restart_required=True, admitting=False)
            self._state(False, None if succeeded else "login_failed")
