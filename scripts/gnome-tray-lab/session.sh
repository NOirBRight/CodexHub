#!/usr/bin/env bash
set -euo pipefail
: "${CODEXHUB_TRAY_LAB:?}"
export XDG_RUNTIME_DIR="$CODEXHUB_TRAY_LAB/runtime"
export XDG_CONFIG_HOME="$CODEXHUB_TRAY_LAB/user/config"
export XDG_DATA_HOME="$CODEXHUB_TRAY_LAB/user/data"
export XDG_CACHE_HOME="$CODEXHUB_TRAY_LAB/user/cache"
export XDG_CURRENT_DESKTOP=ubuntu:GNOME
export LIBGL_ALWAYS_SOFTWARE=1
unset DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS SESSION_MANAGER
exec dbus-run-session --config-file="$CODEXHUB_TRAY_LAB/bus.conf" -- bash "$CODEXHUB_TRAY_LAB/desktop.sh"
