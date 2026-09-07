"""Verify the running app's GNOME dock identity against an existing icon cache.

Runs a separate headless GNOME session with synthetic user data. No screenshots,
user configuration, credentials, or local paths are included in the report.
"""
from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


# A dock probe runs a real desktop application.  Keep its process tree entirely
# separate from the operator's credentials, proxies, desktop settings and
# config roots.  D-Bus is added by dbus-run-session before the inner process.
ENVIRONMENT_PASSTHROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "XDG_CONFIG_DIRS",
    "XDG_DATA_DIRS",
    "DBUS_SESSION_BUS_ADDRESS",
)


def _safe_environment(**overrides: str) -> dict[str, str]:
    """Return the minimal environment required by the isolated GNOME probe."""

    environment = {
        name: value
        for name in ENVIRONMENT_PASSTHROUGH
        if (value := os.environ.get(name))
    }
    environment.update(overrides)
    return environment


def _copy_portable_candidate(source: Path, destination: Path) -> Path:
    """Copy one portable tree so an upgrade has a distinct executable path."""

    shutil.copytree(source, destination, symlinks=True)
    candidate = destination / "CodexHub"
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError("portable candidate is missing an executable CodexHub")
    for required in ("config/providers.toml", "src-python/codex_proxy.py"):
        if not (destination / required).is_file():
            raise RuntimeError(f"portable candidate is missing {required}")
    return candidate


def _assert_current_launcher(home: Path, binary: Path) -> None:
    """Ensure the managed launcher now selects this candidate, without logging paths."""

    launcher = home / "data/applications/com.codexhub.app.desktop"
    body = launcher.read_text(encoding="utf-8")
    expected = f"Exec={binary}\n"
    if expected not in body:
        raise RuntimeError("portable upgrade did not update the managed launcher")


def _sanitized_result_lines(output: str) -> list[str]:
    """Keep only the fixed, anonymous probe records from a child session."""

    records: list[str] = []
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(record, dict)
            and record.get("phase") in {"first_launch", "portable_upgrade"}
            and isinstance(record.get("passed"), bool)
            and isinstance(record.get("identity"), list)
        ):
            records.append(
                json.dumps(
                    {"phase": record["phase"], "passed": record["passed"]},
                    sort_keys=True,
                )
            )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin', required=True, type=Path)
    args = parser.parse_args()
    binary = args.bin.resolve()
    if os.environ.get('CODEXHUB_DOCK_TEST_SESSION') != '1':
        env = _safe_environment(CODEXHUB_DOCK_TEST_SESSION='1')
        try:
            completed = subprocess.run(
                ['dbus-run-session', '--', sys.executable, __file__, '--bin', str(binary)],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            print("isolated GNOME dock icon test timed out", file=sys.stderr)
            return 1
        for record in _sanitized_result_lines(completed.stdout):
            print(record, flush=True)
        if completed.returncode:
            print("isolated GNOME dock icon test failed", file=sys.stderr)
            return 1
        return 0
    for command in ['gnome-shell', 'gsettings', 'gdbus', 'gtk-update-icon-cache']:
        if shutil.which(command) is None:
            raise RuntimeError(f'Missing test dependency: {command}')
    shell_version = subprocess.check_output(['gnome-shell', '--version'], text=True).split()[-1].split('.')[0]
    with tempfile.TemporaryDirectory(prefix='codexhub-dock-test-') as directory:
        home = Path(directory)
        env = _safe_environment(HOME=directory, XDG_CONFIG_HOME=str(home / 'config'),
                                XDG_CACHE_HOME=str(home / 'cache'), XDG_DATA_HOME=str(home / 'data'),
                                XDG_RUNTIME_DIR=str(home / 'runtime'), CODEX_HOME=str(home / 'codex'),
                                CODEXHUB_RUNTIME_HOME=str(home / 'state'), GDK_BACKEND='wayland',
                                WAYLAND_DISPLAY='codexhub-dock-test', LIBGL_ALWAYS_SOFTWARE='1',
                                NO_AT_BRIDGE='1', GSETTINGS_BACKEND='keyfile')
        (home / 'runtime').mkdir(mode=0o700)
        extension = home / 'data/gnome-shell/extensions/codexhub-dock-test@local'
        extension.mkdir(parents=True)
        (extension / 'metadata.json').write_text(json.dumps({
            'uuid': 'codexhub-dock-test@local', 'name': 'Isolated dock test',
            'description': 'Test only', 'shell-version': [shell_version]}))
        (extension / 'extension.js').write_text(r"""
import Shell from 'gi://Shell';
import St from 'gi://St';
import Gio from 'gi://Gio';
import GdkPixbuf from 'gi://GdkPixbuf';
export default class Probe {
 enable() {
  global.context.unsafe_mode = true;
  global.codexhubDockIdentity = () => global.get_window_actors()
   .filter(a => a.meta_window.get_title() === 'CodexHub').map(a => {
    const app = Shell.WindowTracker.get_default().get_window_app(a.meta_window);
    const gicon = app?.get_app_info()?.get_icon();
    const icon = gicon?.to_string();
    let iconFound = false;
    if (gicon instanceof Gio.FileIcon) {
      try { iconFound = GdkPixbuf.Pixbuf.new_from_file(gicon.get_file().get_path()).get_width() > 0; }
      catch (_) {}
    } else { iconFound = St.IconTheme.new().has_icon(icon ?? 'missing'); }
    return {appId: app?.get_id(), windowBacked: app?.is_window_backed(),
            iconFound, branded: /(?:^|\/)codexhub-[a-f0-9]{12}(?:\.png)?$/.test(icon ?? '')};
   });
 }
 disable() { delete global.codexhubDockIdentity; global.context.unsafe_mode = false; }
}
""")
        theme = home / 'data/icons/hicolor'
        theme.mkdir(parents=True)
        shutil.copy2('/usr/share/icons/hicolor/index.theme', theme / 'index.theme')
        for size in ['32x32', '128x128', '256x256', '256x256@2', '512x512']:
            (theme / size / 'apps').mkdir(parents=True)
        # A populated cache is essential: an empty theme may produce no cache,
        # silently turning this into a first-install test that misses the bug.
        shutil.copy2(Path(__file__).resolve().parents[1] / 'src-tauri/icons/128x128.png',
                     theme / '128x128/apps/old-icon.png')
        subprocess.run(['gtk-update-icon-cache', '--force', str(theme)], env=env,
                       check=True, capture_output=True)
        assert (theme / 'icon-theme.cache').is_file()
        subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'enabled-extensions',
                        "['codexhub-dock-test@local']"], env=env, check=True, capture_output=True)
        processes = []
        with (home / 'private.log').open('w') as log:
            try:
                shell = subprocess.Popen(['gnome-shell', '--headless', '--wayland',
                    '--wayland-display=codexhub-dock-test', '--virtual-monitor=1024x768',
                    '--debug-control'], env=env, stdout=log, stderr=log)
                processes.append(shell)
                deadline = time.monotonic() + 15
                while not (home / 'runtime/codexhub-dock-test').exists():
                    if shell.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Isolated GNOME failed to start')
                    time.sleep(.1)
                time.sleep(3)
                old_binary = _copy_portable_candidate(binary.parent, home / 'portable-old')
                new_binary = _copy_portable_candidate(binary.parent, home / 'portable-new')
                for phase, candidate in (("first_launch", old_binary), ("portable_upgrade", new_binary)):
                    app = subprocess.Popen([str(candidate)], cwd=home, env=env, stdout=log, stderr=log)
                    processes.append(app)
                    time.sleep(8)
                    result = subprocess.check_output(['gdbus', 'call', '--session', '--dest',
                        'org.gnome.Shell', '--object-path', '/org/gnome/Shell', '--method',
                        'org.gnome.Shell.Eval', 'global.codexhubDockIdentity()'],
                        env=env, text=True, timeout=10)
                    success, payload = ast.literal_eval(result.replace('(true,', '(True,').replace('(false,', '(False,'))
                    identities = json.loads(payload) if success else []
                    passed = len(identities) == 1 and identities[0] == {
                        'appId': 'com.codexhub.app.desktop', 'windowBacked': False,
                        'iconFound': True, 'branded': True}
                    _assert_current_launcher(home, candidate)
                    print(json.dumps({'phase': phase, 'passed': passed,
                                      'identity': identities}), flush=True)
                    if not passed:
                        raise RuntimeError('GNOME dock cannot resolve the running application icon')
                    app.terminate()
                    app.wait(timeout=5)
                return 0
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
