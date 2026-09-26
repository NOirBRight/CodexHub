#!/usr/bin/env bash
# Package the native portable layout; GTK/WebKit and their loaders stay on the host.
set -euo pipefail
[[ "$#" -eq 3 ]] || { echo "usage: $0 RELEASE_DIR APPDIR ICON" >&2; exit 2; }
release_dir="$1"
app_dir="$2"
icon="$3"
for required in codexhub config/providers.toml config/official_fast_variants.json src-python/codex_proxy.py scripts/xai_device_login.py; do
  [[ -f "$release_dir/$required" ]] || { echo "missing native resource: $required" >&2; exit 1; }
done
mkdir -p "$app_dir"
cp -a "$release_dir/codexhub" "$app_dir/CodexHub"
for resource in config src-python python scripts; do
  if [[ -e "$release_dir/$resource" ]]; then
    cp -a "$release_dir/$resource" "$app_dir/"
  fi
done
cp "$icon" "$app_dir/CodexHub.png"
cat > "$app_dir/AppRun" <<'RUN'
#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$app_dir/CodexHub" "$@"
RUN
chmod +x "$app_dir/AppRun"
cat > "$app_dir/CodexHub.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=CodexHub
Exec=CodexHub
Icon=CodexHub
Terminal=false
Categories=Development;
StartupWMClass=com.codexhub.app
DESKTOP
cat > "$app_dir/LINUX_RUNTIME.txt" <<'NOTE'
Native Linux package: requires the host GTK 3, WebKitGTK 4.1, libayatana-appindicator,
lsof and Python 3.13+. Uses the build host's glibc baseline (Omarchy/Arch).
GTK, graphics drivers, image loaders and their sandbox helpers come from the host.
NOTE
