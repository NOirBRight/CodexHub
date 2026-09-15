#!/usr/bin/env bash
set -euo pipefail
: "${CODEXHUB_TRAY_LAB:?}"
printf '%s' "$DBUS_SESSION_BUS_ADDRESS" > "$CODEXHUB_TRAY_LAB/bus-address"
export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
gsettings set org.gnome.shell disable-user-extensions false
gsettings set org.gnome.shell enabled-extensions "['ubuntu-appindicators@ubuntu.com', 'tray-lab-observer@test.local']"
gsettings set org.gnome.desktop.interface color-scheme "${CODEXHUB_TRAY_COLOR_SCHEME:-prefer-light}"
gsettings set org.gnome.desktop.interface gtk-theme 'Yaru'
gsettings set org.gnome.desktop.session idle-delay 0
bash "$CODEXHUB_TRAY_LAB/clients.sh" &
exec gnome-shell --headless --virtual-monitor 1280x900 --wayland --wayland-display wayland-tray-lab --mode ubuntu --debug-control
