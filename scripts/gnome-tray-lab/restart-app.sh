#!/usr/bin/env bash
set -euo pipefail
source "$CODEXHUB_TRAY_LAB/guard.sh"
pkill -x codexhub || true
export WAYLAND_DISPLAY=wayland-tray-lab
export GDK_BACKEND=wayland
export CODEXHUB_RUNTIME_HOME="$CODEXHUB_TRAY_LAB/user/app-runtime"
export CODEXHUB_RESOURCE_ROOT=/mnt
exec "$CODEXHUB_TRAY_APP" >> "$CODEXHUB_TRAY_LAB/artifacts/restarts.log" 2>&1
