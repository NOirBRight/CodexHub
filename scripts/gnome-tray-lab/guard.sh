#!/usr/bin/env bash
# Refuse to run app/reference helpers in the host PID namespace.
if [[ -r /proc/1/comm ]] && grep -qx systemd /proc/1/comm; then
  echo "error: gnome-tray-lab helper refused to run on the host PID namespace" >&2
  exit 2
fi
