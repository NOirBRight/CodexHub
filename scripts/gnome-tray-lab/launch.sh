#!/usr/bin/env bash
set -euo pipefail
: "${CODEXHUB_TRAY_LAB:?}"
: "${CODEXHUB_TRAY_REPO:?}"
exec bwrap \
  --ro-bind / / \
  --tmpfs /tmp \
  --bind "$CODEXHUB_TRAY_LAB" "$CODEXHUB_TRAY_LAB" \
  --tmpfs /home/noirbright \
  --ro-bind "$CODEXHUB_TRAY_REPO" /mnt \
  --tmpfs /run \
  --dev /dev \
  --proc /proc \
  --unshare-pid \
  --unshare-net \
  --unshare-ipc \
  --die-with-parent \
  --ro-bind "$CODEXHUB_TRAY_LAB/extensions" /usr/share/gnome-shell/extensions \
  --setenv CODEXHUB_TRAY_LAB "$CODEXHUB_TRAY_LAB" \
  --setenv CODEXHUB_TRAY_APP "$CODEXHUB_TRAY_APP" \
  --setenv CODEXHUB_TRAY_COLOR_SCHEME "${CODEXHUB_TRAY_COLOR_SCHEME:-prefer-light}" \
  --chdir "$CODEXHUB_TRAY_LAB" \
  bash "$CODEXHUB_TRAY_LAB/session.sh"
