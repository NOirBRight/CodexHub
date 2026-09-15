#!/usr/bin/env bash
set -euo pipefail
: "${CODEXHUB_TRAY_LAB:?}"
addr="unix:path=${CODEXHUB_TRAY_LAB}/runtime/bus"
items="$(gdbus call --address "$addr" --dest org.kde.StatusNotifierWatcher --object-path /StatusNotifierWatcher --method org.freedesktop.DBus.Properties.Get -- org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems)"
printf 'items: %s\n' "$items"
echo "$items" | tr "[]<>,()'\" " '\n' | grep '@/org/' | while read -r item; do
  dest="${item%%@*}"
  path="${item#*@}/Menu"
  printf 'dest=%s path=%s\n' "$dest" "$path"
  gdbus call --address "$addr" --dest "$dest" --object-path "$path" --method com.canonical.dbusmenu.GetLayout -- 0 -1 '[]' || true
done
