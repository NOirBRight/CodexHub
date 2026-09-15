#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=guard.sh
source "$CODEXHUB_TRAY_LAB/guard.sh"
# shellcheck source=tray-app-env.sh
source "$CODEXHUB_TRAY_LAB/tray-app-env.sh"
mkdir -p "$CODEXHUB_RUNTIME_HOME/proxy"
printf '%s\n' '{"auto_start_software":false,"auto_start_gateway":false}' > "$CODEXHUB_RUNTIME_HOME/proxy/settings.json"
for _ in $(seq 1 30); do
  test -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" && break
  sleep 1
done
if [[ ! -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]]; then
  echo "error: wayland socket did not appear" >&2
  exit 1
fi
sleep 3
"$CODEXHUB_TRAY_LAB/reference" > "$CODEXHUB_TRAY_LAB/artifacts/reference.log" 2>&1 &
"$CODEXHUB_TRAY_APP" > "$CODEXHUB_TRAY_LAB/artifacts/app.log" 2>&1 &
wait
