"""Exercise CodexHub in a separate headless Hyprland (Lua-config) session.

Requires Hyprland, grim, wlrctl and a desktop StatusNotifierWatcher. Uses the
operator's tray bus but never moves or clicks the operator's pointer.
"""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen


def run(args, env=None):
    return subprocess.check_output(
        args, env=env, text=True, stderr=subprocess.PIPE, timeout=15
    )


def wait_for(check, description, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError(f"Timed out: {description}")


def stop(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("test-results/hyprland-e2e.json")
    )
    args = parser.parse_args()
    binary = args.bin.resolve(strict=True)
    for tool in (
        "Hyprland",
        "hyprctl",
        "grim",
        "wlrctl",
        "gdbus",
        "dbus-run-session",
        "lsof",
    ):
        if not shutil.which(tool):
            parser.error(f"missing dependency: {tool}")
    # The desktop bridge is not namespaced; refuse to test against another app.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 1421)) == 0:
            parser.error(
                "close the existing CodexHub before this isolated test (port 1421 occupied)"
            )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    report = {
        "schema": "codexhub.hyprland-e2e.v1",
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "checks": [],
    }
    compositor = app = None
    # Keep the runtime path short: Hyprland's instance name uses most of the
    # 108-byte Unix socket path limit.
    root = Path(tempfile.mkdtemp(prefix="ch-"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        config = root / "hyprland.lua"
        config.write_text(
            'hl.monitor({output="",mode="1280x960@60",position="auto",scale=1})\n'
            "hl.config({animations={enabled=false},misc={disable_hyprland_logo=true,disable_splash_rendering=true},input={follow_mouse=0},general={gaps_in=0,gaps_out=0}})\n"
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            in (
                "PATH",
                "LANG",
                "LC_ALL",
                "DBUS_SESSION_BUS_ADDRESS",
                "XDG_DATA_DIRS",
                "CODEXHUB_PYTHON",
                "CODEXHUB_PROXY_PYTHON",
                "CODEXHUB_E2E_PYTHON",
            )
        }
        env.update(
            HOME=str(root),
            XDG_RUNTIME_DIR=str(root),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_DATA_HOME=str(root / "data"),
            XDG_CACHE_HOME=str(root / "cache"),
            HYPRLAND_NO_SD_VARS="1",
            HYPRLAND_NO_SD_NOTIFY="1",
            WAYLAND_DISPLAY=str(
                Path(os.environ["XDG_RUNTIME_DIR"]) / os.environ["WAYLAND_DISPLAY"]
            ),
        )
        with (root / "compositor.log").open("w") as log:
            compositor = subprocess.Popen(
                ["dbus-run-session", "--", "Hyprland", "--config", str(config)],
                env=env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        ipc = wait_for(
            lambda: next((root / "hypr").glob("*/.socket.sock"), None), "Hyprland IPC"
        )
        display = wait_for(
            lambda: next((p for p in root.glob("wayland-*") if p.is_socket()), None),
            "Wayland socket",
        )
        env.update(
            HYPRLAND_INSTANCE_SIGNATURE=ipc.parent.name,
            WAYLAND_DISPLAY=display.name,
            GDK_BACKEND="wayland",
            CODEX_HOME=str(root / "codex"),
            CODEXHUB_RUNTIME_HOME=str(root / "hub"),
        )
        run(["hyprctl", "output", "create", "headless"], env)
        monitors = json.loads(run(["hyprctl", "monitors", "-j"], env))
        for monitor in monitors:
            if not monitor["name"].startswith("HEADLESS-"):
                run(["hyprctl", "output", "remove", monitor["name"]], env)
        monitors = json.loads(run(["hyprctl", "monitors", "-j"], env))
        if not monitors or any(not m["name"].startswith("HEADLESS-") for m in monitors):
            raise RuntimeError(
                "refusing pointer input outside an exclusively headless test session"
            )
        proxy = root / "hub/proxy"
        proxy.mkdir(parents=True)
        (proxy / "settings.json").write_text(
            json.dumps(
                {
                    "auto_start_software": False,
                    "auto_start_gateway": False,
                    "auto_sync_catalog": False,
                    "auto_sync_clients": False,
                    "auto_sync_history": False,
                    "proxy_port": port,
                    "gateway_bind_address": "127.0.0.1",
                    "gateway_client_key": "isolated-hyprland-test-key",
                }
            )
        )
        with (root / "app.log").open("w") as log:
            app = subprocess.Popen(
                [str(binary)], env=env, stdout=log, stderr=log, start_new_session=True
            )

        def window():
            return next(
                (
                    w
                    for w in json.loads(run(["hyprctl", "clients", "-j"], env))
                    if w["class"] == "com.codexhub.app" and w["mapped"]
                ),
                None,
            )

        def health():
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    return response.status == 200
            except OSError:
                return False

        def passed(name, condition=True):
            if not condition:
                raise RuntimeError(name)
            report["checks"].append(name)
            print("PASS:", name, flush=True)

        w = wait_for(window, "mapped CodexHub window")
        passed("native_wayland_window", not w["xwayland"] and w["acceptsInput"])

        def click(x, y):
            for _ in range(3):
                pos = json.loads(run(["hyprctl", "cursorpos", "-j"], env))
                run(
                    ["wlrctl", "pointer", "move", str(x - pos["x"]), str(y - pos["y"])],
                    env,
                )
                time.sleep(0.1)
            pos = json.loads(run(["hyprctl", "cursorpos", "-j"], env))
            if abs(pos["x"] - x) > 2 or abs(pos["y"] - y) > 2:
                raise RuntimeError("virtual pointer did not reach the target")
            run(["wlrctl", "pointer", "click", "left"], env)
            time.sleep(0.5)

        def pixels():
            image = subprocess.check_output(
                ["grim", "-t", "ppm", "-s", "1", "-"], env=env, timeout=5
            )
            return image.split(b"\n", 3)[3]

        def changed(a, b):
            return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) / len(a) > 0.1

        time.sleep(1)
        click(78, 75)
        before = pixels()
        click(650, 75)
        settings = pixels()
        passed("settings_native_pointer_input", changed(before, settings))
        run(["grim", "-s", "1", str(args.output.with_suffix(".png").resolve())], env)
        click(78, 75)
        passed("workspace_native_pointer_input", changed(settings, pixels()))
        watcher = run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.kde.StatusNotifierWatcher",
                "--object-path",
                "/StatusNotifierWatcher",
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                "org.kde.StatusNotifierWatcher",
                "RegisteredStatusNotifierItems",
            ]
        )
        item = re.search(
            r"'([^']+/org/ayatana/NotificationItem/tray_icon_tray_app_codexhub)'",
            watcher,
        )
        if not item:
            raise RuntimeError("CodexHub tray item missing")
        bus, path = item.group(1).split("/", 1)
        bus_pid = run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.GetConnectionUnixProcessID",
                bus,
            ]
        )
        if re.search(r"uint32 (\d+)", bus_pid).group(1) != str(w["pid"]):
            raise RuntimeError("tray item belongs to a different application process")
        menu_path = "/" + path + "/Menu"
        base = [
            "gdbus",
            "call",
            "--session",
            "--dest",
            bus,
            "--object-path",
            menu_path,
            "--method",
        ]
        layout = run(
            base
            + [
                "com.canonical.dbusmenu.GetLayout",
                "--",
                "0",
                "-1",
                "['label','enabled']",
            ]
        )
        ids = {
            label: int(id)
            for id, label in re.findall(r"\((\d+), \{'label': <'([^']+)'>", layout)
        }
        labels = [
            "Show CodexHub",
            "Start Gateway",
            "Stop Gateway",
            "Restart Gateway",
            "Exit",
        ]
        passed("tray_menu_labels", all(label in ids for label in labels))

        def menu(label):
            run(
                base
                + [
                    "com.canonical.dbusmenu.Event",
                    str(ids[label]),
                    "clicked",
                    "<0>",
                    "0",
                ]
            )

        menu("Start Gateway")
        wait_for(health, "Gateway start")
        passed("tray_start_gateway")
        run(
            [
                "hyprctl",
                "dispatch",
                f'hl.dsp.window.close({{window="pid:{w["pid"]}"}})',
            ],
            env,
        )
        wait_for(lambda: not window(), "close to tray")
        passed("close_keeps_gateway", health())
        menu("Show CodexHub")
        wait_for(window, "tray restore")
        passed("tray_restores_window")

        def owner():
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError("listener ownership inspection failed")
            return result.stdout.strip()

        old_pid = owner()
        menu("Restart Gateway")
        wait_for(
            lambda: health() and (new_pid := owner()) and new_pid != old_pid,
            "Gateway restart",
        )
        passed("tray_restarts_gateway")
        menu("Stop Gateway")
        wait_for(lambda: not health(), "Gateway stop")
        passed("tray_stop_gateway")
        menu("Start Gateway")
        wait_for(health, "Gateway second start")
        menu("Exit")
        wait_for(lambda: app.poll() is not None and not health(), "application exit")
        passed("exit_stops_gateway")
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error).replace(str(root), "<isolated-home>"))
        if app is not None:
            try:
                run(
                    [
                        "grim",
                        "-s",
                        "1",
                        str(
                            args.output.with_name(
                                args.output.stem + "-failure.png"
                            ).resolve()
                        ),
                    ],
                    env,
                )
                from urllib.request import Request

                with urlopen(
                    Request(
                        "http://127.0.0.1:1421/api/invoke",
                        data=b'{"command":"get_status","args":{}}',
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=5,
                ) as response:
                    report["failure_status"] = json.load(response)
            except Exception:
                pass
    finally:
        stop(app)
        stop(compositor)
        for name in ("app.log", "compositor.log"):
            if (root / name).exists():
                args.output.with_name(args.output.stem + "-" + name).write_text(
                    (root / name)
                    .read_text(errors="replace")
                    .replace(str(root), "<isolated-home>")
                )
        shutil.rmtree(root, ignore_errors=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
