import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.e2e_linux_window_input import (
    drawer_cycle_has_required_transitions,
    visual_frame_has_ui_content,
)


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux packaging and pointer contract")


def test_full_linux_suite_public_interface_lists_physical_pointer_gate() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify-linux.sh"), "--list"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert "Linux physical pointer input E2E" in result.stdout
    assert "90s watchdog" in result.stdout
    assert "xvfb-run" in result.stdout
    assert "xwininfo" in result.stdout


def test_pointer_input_e2e_public_self_test_rejects_tray_window() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "codexhub-python.sh"), str(ROOT / "scripts" / "e2e_linux_window_input.py"), "--self-test"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: Linux pointer-input E2E self-test" in result.stdout


def test_pointer_input_e2e_accepts_a_complete_drawer_cycle_after_async_page_updates() -> None:
    assert drawer_cycle_has_required_transitions(0.534, 0.560, 0.421, 0.487)
    assert not drawer_cycle_has_required_transitions(0.534, 0.560, 0.020, 0.487)


def test_pointer_input_e2e_rejects_stable_blank_frames_before_clicking() -> None:
    assert not visual_frame_has_ui_content((0xFFFFFF,) * 128)
    assert not visual_frame_has_ui_content((0xFFFFFF,) * 124 + (0xEEEEEE,) * 4)
    assert visual_frame_has_ui_content(tuple(range(16)) * 8)


def test_deb_package_runs_recoverable_launcher_migration_at_install_time(tmp_path: Path) -> None:
    config = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    legacy = """[Desktop Entry]
Type=Application
Name=CodexHub
Comment=CodexHub desktop backend and CLI
Exec=/usr/bin/codexhub
Icon=codexhub
Terminal=false
Categories=Development;
StartupNotify=true
StartupWMClass=com.codexhub.app
X-GNOME-UsesNotifications=true
"""
    apps = tmp_path / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    for name in ("codexhub.desktop", "com.codexhub.app.desktop"):
        (apps / name).write_text(legacy, encoding="utf-8")
    custom = apps / "custom.desktop"
    custom.write_text("[Desktop Entry]\nName=Custom\nExec=/custom/app\n", encoding="utf-8")

    assert config["bundle"]["linux"]["deb"]["postInstallScript"] == "linux/deb-postinstall.sh"
    assert "util-linux" in config["bundle"]["linux"]["deb"]["depends"]
    result = subprocess.run(
        ["sh", str(hook), "--test-home", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert custom.exists()
    for name in ("codexhub.desktop", "com.codexhub.app.desktop"):
        assert not (apps / name).exists()
        assert (apps / f"{name}.codexhub-legacy-backup").read_text(encoding="utf-8") == legacy


def test_deb_postinstall_pins_the_package_launcher_to_usr_bin(tmp_path: Path) -> None:
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    launcher = tmp_path / "CodexHub.desktop"
    launcher.write_text(
        """[Desktop Entry]
Categories=
Comment=CodexHub desktop backend and CLI
Exec=codexhub
StartupWMClass=codexhub
Icon=codexhub
Name=CodexHub
Terminal=false
Type=Application
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", str(hook), "--test-system-launcher", str(launcher)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    content = launcher.read_text(encoding="utf-8")
    assert "Exec=/usr/bin/codexhub\n" in content
    assert "StartupWMClass=com.codexhub.app\n" in content
    assert "Exec=codexhub\n" not in content


def test_deb_launcher_migration_archives_the_owned_legacy_appimage_command(tmp_path: Path) -> None:
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    (tmp_path / ".local" / "share" / "applications").mkdir(parents=True)
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir()
    applications = tmp_path / "Applications"
    applications.mkdir()
    appimage = applications / "CodexHub.AppImage"
    appimage.write_bytes(b"legacy appimage")
    command = local_bin / "codexhub"
    command.symlink_to(appimage)

    result = subprocess.run(
        ["sh", str(hook), "--test-home", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not command.exists()
    backup = command.with_name("codexhub.codexhub-legacy-backup")
    assert backup.is_symlink()
    assert backup.resolve() == appimage
    assert appimage.read_bytes() == b"legacy appimage"


def test_deb_launcher_migration_preserves_a_custom_local_command(tmp_path: Path) -> None:
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    (tmp_path / ".local" / "share" / "applications").mkdir(parents=True)
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir()
    command = local_bin / "codexhub"
    command.symlink_to("/custom/codexhub")

    result = subprocess.run(
        ["sh", str(hook), "--test-home", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert command.is_symlink()
    assert os.readlink(command) == "/custom/codexhub"
    assert not command.with_name("codexhub.codexhub-legacy-backup").exists()


def test_deb_launcher_migration_refuses_symlinked_home_ancestors(tmp_path: Path) -> None:
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    home = tmp_path / "home"
    external = tmp_path / "external"
    apps = external / "share" / "applications"
    home.mkdir()
    apps.mkdir(parents=True)
    (home / ".local").symlink_to(external, target_is_directory=True)
    launcher = apps / "codexhub.desktop"
    launcher.write_text(
        """[Desktop Entry]
Type=Application
Name=CodexHub
Comment=CodexHub desktop backend and CLI
Exec=/old/CodexHub
Icon=codexhub
Terminal=false
Categories=Development;
StartupNotify=true
StartupWMClass=com.codexhub.app
X-GNOME-UsesNotifications=true
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", str(hook), "--test-home", str(home)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert launcher.exists()
    assert not launcher.with_name(f"{launcher.name}.codexhub-legacy-backup").exists()


def test_deb_launcher_migration_preserves_same_name_custom_exec(tmp_path: Path) -> None:
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    apps = tmp_path / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    launcher = apps / "codexhub.desktop"
    custom = """[Desktop Entry]
Type=Application
Name=CodexHub
Comment=CodexHub desktop backend and CLI
Exec=/custom/codexhub
Icon=codexhub
Terminal=false
Categories=Development;
StartupNotify=true
StartupWMClass=com.codexhub.app
X-GNOME-UsesNotifications=true
"""
    launcher.write_text(custom, encoding="utf-8")

    result = subprocess.run(
        ["sh", str(hook), "--test-home", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert launcher.read_text(encoding="utf-8") == custom
    assert not launcher.with_name(f"{launcher.name}.codexhub-legacy-backup").exists()


def test_deb_launcher_migration_enumerates_users_without_invoking_uid(tmp_path: Path) -> None:
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    homes = tmp_path / "homes"
    home = homes / "desktop-user"
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    launcher = apps / "codexhub.desktop"
    legacy = """[Desktop Entry]
Type=Application
Name=CodexHub
Comment=CodexHub desktop backend and CLI
Exec=/usr/bin/codexhub
Icon=codexhub
Terminal=false
Categories=Development;
StartupNotify=true
StartupWMClass=com.codexhub.app
X-GNOME-UsesNotifications=true
"""
    launcher.write_text(legacy, encoding="utf-8")
    passwd = tmp_path / "passwd"
    passwd.write_text(
        f"desktop-user:x:{os.getuid()}:{os.getgid()}::{home}:/bin/bash\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["sh", str(hook), "--test-passwd", str(passwd), str(homes)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert not launcher.exists()
    backup = launcher.with_name(f"{launcher.name}.codexhub-legacy-backup")
    assert backup.read_text(encoding="utf-8") == legacy


def test_deb_launcher_migration_dispatches_complete_unprivileged_cleanup(tmp_path: Path) -> None:
    hook = ROOT / "src-tauri" / "linux" / "deb-postinstall.sh"
    homes = tmp_path / "homes"
    home = homes / "different-user"
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    launcher = apps / "codexhub.desktop"
    legacy = """[Desktop Entry]
Type=Application
Name=CodexHub
Comment=CodexHub desktop backend and CLI
Exec=/usr/bin/codexhub
Icon=codexhub
Terminal=false
Categories=Development;
StartupNotify=true
StartupWMClass=com.codexhub.app
X-GNOME-UsesNotifications=true
"""
    launcher.write_text(legacy, encoding="utf-8")
    passwd = tmp_path / "passwd"
    target_uid = os.getuid() + 1
    target_gid = os.getgid() + 1
    passwd.write_text(
        f"different-user:x:{target_uid}:{target_gid}::{home}:/bin/bash\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    setpriv = fake_bin / "setpriv"
    setpriv.write_text(
        """#!/bin/sh
printf '%s\n' "$@" > "$CODEXHUB_SETPRIV_RECORD"
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
[ "${1-}" = "--" ] || exit 2
shift
exec "$@"
""",
        encoding="utf-8",
    )
    setpriv.chmod(0o755)
    record = tmp_path / "setpriv.args"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["CODEXHUB_SETPRIV_RECORD"] = str(record)

    result = subprocess.run(
        ["sh", str(hook), "--test-passwd", str(passwd), str(homes)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert record.read_text(encoding="utf-8").splitlines() == [
        "--reuid",
        str(target_uid),
        "--regid",
        str(target_gid),
        "--clear-groups",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--reset-env",
        "--",
        str(hook),
        "--cleanup-home",
        str(home),
    ]
    assert not launcher.exists()
    backup = launcher.with_name(f"{launcher.name}.codexhub-legacy-backup")
    assert backup.read_text(encoding="utf-8") == legacy
