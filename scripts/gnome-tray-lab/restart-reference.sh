#!/usr/bin/env bash
set -euo pipefail
source "$CODEXHUB_TRAY_LAB/guard.sh"
pkill -x reference || true
export WAYLAND_DISPLAY=wayland-tray-lab
export GDK_BACKEND=wayland
exec "$CODEXHUB_TRAY_LAB/reference" >> "$CODEXHUB_TRAY_LAB/artifacts/reference-restarts.log" 2>&1
