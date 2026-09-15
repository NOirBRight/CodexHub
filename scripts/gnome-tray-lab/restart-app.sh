#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=guard.sh
source "$CODEXHUB_TRAY_LAB/guard.sh"
# shellcheck source=tray-app-env.sh
source "$CODEXHUB_TRAY_LAB/tray-app-env.sh"
pkill -x codexhub || true
exec "$CODEXHUB_TRAY_APP" >> "$CODEXHUB_TRAY_LAB/artifacts/restarts.log" 2>&1
