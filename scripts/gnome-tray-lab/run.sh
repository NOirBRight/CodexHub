#!/usr/bin/env bash
# Isolated GNOME tray readability harness.
# Talks only to a private D-Bus and never evaluates the host GNOME Shell.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
lab_root="${CODEXHUB_TRAY_LAB:-/tmp/codexhub-tray-lab}"
app_rel="src-tauri/target/release/codexhub"
repeats=10
expect="report"
theme="prefer-light"
timeout_secs=420
deb_version="50.26.04.7ubuntu"

usage() {
  cat <<'HINT'
Usage: scripts/gnome-tray-lab/run.sh [options]
  --expect-failures   Exit 0 only if CodexHub empty/unreadable labels are captured
  --expect-pass       Exit 0 only if every CodexHub and reference sample passes
  --repeats N         CodexHub restart samples (default 10)
  --theme light|dark  Isolated color-scheme (default light)
  --lab-root PATH     Disposable lab directory
  --bin PATH          CodexHub binary inside the repo worktree
  --timeout SECS      Overall timeout (default 420)
HINT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-failures) expect=failures; shift ;;
    --expect-pass) expect=pass; shift ;;
    --repeats) repeats="${2:?}"; shift 2 ;;
    --theme)
      case "${2:?}" in
        light) theme=prefer-light ;;
        dark) theme=prefer-dark ;;
        *) echo "unsupported theme $2" >&2; exit 2 ;;
      esac
      shift 2
      ;;
    --lab-root) lab_root="${2:?}"; shift 2 ;;
    --bin) app_rel="${2:?}"; shift 2 ;;
    --timeout) timeout_secs="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$app_rel" = /* ]]; then
  app_host="$app_rel"
else
  app_host="$repo_root/$app_rel"
fi
if [[ ! -x "$app_host" ]]; then
  echo "error: missing CodexHub binary $app_host" >&2
  exit 2
fi
case "$app_host" in
  "$repo_root"/*) ;;
  *) echo "error: binary must live inside the repo so the sandbox can read it at /mnt" >&2; exit 2 ;;
esac
app_sandbox="/mnt/${app_host#"$repo_root"/}"

export CODEXHUB_TRAY_LAB="$lab_root"
export CODEXHUB_TRAY_REPO="$repo_root"
export CODEXHUB_TRAY_APP="$app_sandbox"
export CODEXHUB_TRAY_COLOR_SCHEME="$theme"

eval_js() {
  gjs -m "$lab_root/eval.js" "$1"
}

wait_for() {
  local name="$1"
  local tries="$2"
  shift 2
  local n=0
  until "$@"; do
    n=$((n + 1))
    if [[ "$n" -ge "$tries" ]]; then
      echo "error: timed out waiting for $name" >&2
      return 1
    fi
    sleep 1
  done
}

record_env() {
  local env_file="$lab_root/artifacts/environment.json"
  local binary_sha gnome_ver ext_deb ext_hash yaru_ver tauri_ver tray_ver muda_ver
  binary_sha="$(sha256sum "$app_host" | awk '{print $1}')"
  gnome_ver="$(gnome-shell --version)"
  ext_deb="$(dpkg-query -W -f '${Version}' gnome-shell-ubuntu-extensions)"
  ext_hash="$(sha256sum "$lab_root/stock/usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com/dbusMenu.js" | awk '{print $1}')"
  yaru_ver="$(dpkg-query -W -f '${Version}' yaru-theme-gnome-shell)"
  tauri_ver="$(awk '/name = "tauri"/{p=1} p&&/version/{gsub(/"/,"",$3); print $3; exit}' "$repo_root/src-tauri/Cargo.lock")"
  tray_ver="$(awk '/name = "tray-icon"/{p=1} p&&/version/{gsub(/"/,"",$3); print $3; exit}' "$repo_root/src-tauri/Cargo.lock")"
  muda_ver="$(awk '/name = "muda"/{p=1} p&&/version/{gsub(/"/,"",$3); print $3; exit}' "$repo_root/src-tauri/Cargo.lock")"
  cat > "$env_file" <<JSON
{
  "recorded_at": "$(date --iso-8601=seconds)",
  "git_sha": "$(git -C "$repo_root" rev-parse HEAD)",
  "git_status": "$(git -C "$repo_root" status --porcelain | tr '\n' ' ')",
  "build_command": "cd frontend && npm ci && npm run build && cd ../src-tauri && cargo build --locked --release --features custom-protocol",
  "binary_host_path": "$app_host",
  "binary_sha256": "$binary_sha",
  "ubuntu": "$(lsb_release -ds)",
  "gnome_shell": "$gnome_ver",
  "gnome_shell_ubuntu_extensions_package": "$ext_deb",
  "stock_dbusmenu_sha256": "$ext_hash",
  "yaru_theme_gnome_shell": "$yaru_ver",
  "tauri": "$tauri_ver",
  "tray_icon": "$tray_ver",
  "muda": "$muda_ver",
  "color_scheme": "$theme",
  "repeats": $repeats,
  "expect": "$expect",
  "contrast_guard_loaded": false,
  "host_desktop_modified": false
}
JSON
}

write_bus_conf() {
  cat > "$lab_root/bus.conf" <<BUS
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
<type>session</type>
<listen>unix:path=${lab_root}/runtime/bus</listen>
<auth>EXTERNAL</auth>
<apparmor mode="disabled"/>
<standard_session_servicedirs/>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow eavesdrop="true"/><allow own="*"/></policy>
</busconfig>
BUS
}

prepare_lab() {
  mkdir -p "$lab_root"/{artifacts,packages,user,runtime,extensions,stock}
  chmod 700 "$lab_root/runtime"
  rm -f "$lab_root/runtime/bus" "$lab_root/bus-address"
  mkdir -p "$lab_root/artifacts"
  find "$lab_root/artifacts" -mindepth 1 -delete
  cp -a "$script_dir/." "$lab_root/"
  write_bus_conf
  if [[ ! -f "$lab_root/packages/gnome-shell-ubuntu-extensions_${deb_version}_all.deb" ]]; then
    (cd "$lab_root/packages" && apt-get download "gnome-shell-ubuntu-extensions=${deb_version}")
  fi
  find "$lab_root/stock" -mindepth 1 -delete
  dpkg-deb -x "$lab_root/packages/gnome-shell-ubuntu-extensions_${deb_version}_all.deb" "$lab_root/stock"
  if [[ -d "$lab_root/extensions/ubuntu-appindicators@ubuntu.com" ]]; then
    find "$lab_root/extensions/ubuntu-appindicators@ubuntu.com" -mindepth 1 -delete
    rmdir "$lab_root/extensions/ubuntu-appindicators@ubuntu.com" || true
  fi
  cp -a "$lab_root/stock/usr/share/gnome-shell/extensions/ubuntu-appindicators@ubuntu.com" "$lab_root/extensions/"
  # Intentional word-split of pkg-config cflags/libs.
  # shellcheck disable=SC2046
  gcc "$lab_root/reference.c" -o "$lab_root/reference" $(pkg-config --cflags --libs gtk+-3.0 ayatana-appindicator3-0.1)
}

cleanup() {
  local pid="${shell_pid:-}"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
}

open_menus() {
  eval_js 'Main.overview.hide(); Object.entries(Main.panel.statusArea).filter(([k])=>k.startsWith("appindicator-")).forEach(([k,s])=>s.menu.open()); true' >/dev/null
}

capture_sample() {
  local stem="$1"
  local json="$lab_root/artifacts/${stem}.json"
  open_menus
  sleep 1
  if ! eval_js "$(cat "$lab_root/inspect-expression.js")" > "$json"; then
    rm -f "$json"
    return 1
  fi
  if [[ ! -s "$json" ]]; then
    rm -f "$json"
    return 1
  fi
  bash "$lab_root/dbus-layout.sh" > "$lab_root/artifacts/${stem}-dbus.txt" 2>&1 || true
  gjs -m "$lab_root/check.js" "$json" | tee "$lab_root/artifacts/${stem}-check.jsonl"
}

restart_codexhub() {
  eval_js "imports.gi.GLib.spawn_command_line_async('bash $lab_root/restart-app.sh'); true" >/dev/null
}

set_theme() {
  local scheme="$1"
  eval_js "imports.gi.Gio.Settings.new('org.gnome.desktop.interface').set_string('color-scheme', '$scheme'); true" >/dev/null
}

shell_pid=""
trap cleanup EXIT

prepare_lab
record_env
timeout "$timeout_secs" bash "$lab_root/launch.sh" > "$lab_root/artifacts/shell.log" 2>&1 &
shell_pid=$!

wait_for "isolated bus" 30 test -S "$lab_root/runtime/bus"
wait_for "gnome-shell eval" 40 eval_js 'true' >/dev/null
wait_for "tray actors" 40 bash -c 'gjs -m "$CODEXHUB_TRAY_LAB/eval.js" "Object.keys(Main.panel.statusArea).filter(k=>k.startsWith(\"appindicator-\")).length" | grep -Eq "^2$"'

jsonl="$lab_root/artifacts/repeat-results.jsonl"
: > "$jsonl"

for n in $(seq 1 "$repeats"); do
  restart_codexhub
  sleep 3
  capture_sample "repeat-$n" >> "$jsonl" || true
done

if [[ "$theme" == prefer-light ]]; then
  set_theme prefer-dark
  sleep 1
  capture_sample "theme-dark" >> "$jsonl" || true
  set_theme prefer-light
  sleep 1
  capture_sample "theme-light-reopen" >> "$jsonl" || true
else
  set_theme prefer-light
  sleep 1
  capture_sample "theme-light" >> "$jsonl" || true
fi

open_menus
sleep 1
capture_sample "reopen" >> "$jsonl" || true

codex_fail=$(awk '/"target":"Show CodexHub"/{print}' "$jsonl" | grep -c '"pass":false' || true)
codex_pass=$(awk '/"target":"Show CodexHub"/{print}' "$jsonl" | grep -c '"pass":true' || true)
ref_fail=$(awk '/"target":"Show Reference"/{print}' "$jsonl" | grep -c '"pass":false' || true)
codex_empty=$(awk '/"target":"Show CodexHub"/{print}' "$jsonl" | grep -c '"kind":"empty-labels"' || true)
codex_contrast=$(awk '/"target":"Show CodexHub"/{print}' "$jsonl" | grep -c '"kind":"low-contrast"' || true)
codex_missing=$(awk '/"target":"Show CodexHub"/{print}' "$jsonl" | grep -c '"kind":"missing' || true)

cat > "$lab_root/artifacts/summary.json" <<JSON
{
  "expect": "$expect",
  "repeats": $repeats,
  "codexhub_pass_lines": $codex_pass,
  "codexhub_fail_lines": $codex_fail,
  "codexhub_empty_label_lines": $codex_empty,
  "codexhub_low_contrast_lines": $codex_contrast,
  "codexhub_missing_lines": $codex_missing,
  "reference_fail_lines": $ref_fail
}
JSON

echo "==> summary"
cat "$lab_root/artifacts/summary.json"

if [[ "$ref_fail" -ne 0 ]]; then
  echo "error: reference AppIndicator failed; do not attribute the failure only to CodexHub" >&2
  exit 1
fi

restart_samples=$(find "$lab_root/artifacts" -maxdepth 1 -name 'repeat-[0-9]*.json' ! -name '*-dbus.json' ! -name '*-check.jsonl' -size +0c | wc -l)
if [[ "$restart_samples" -ne "$repeats" ]]; then
  echo "error: captured $restart_samples restart samples, expected $repeats" >&2
  exit 1
fi

case "$expect" in
  failures)
    if [[ "$codex_empty" -gt 0 ]]; then
      echo "captured $codex_empty CodexHub empty-label failures on the unfixed binary"
      exit 0
    fi
    echo "error: unfixed binary did not produce empty CodexHub labels in $repeats restarts" >&2
    exit 1
    ;;
  pass)
    if [[ "$codex_fail" -eq 0 && "$codex_pass" -ge "$repeats" ]]; then
      exit 0
    fi
    echo "error: expected every CodexHub sample to pass ($codex_pass pass / $codex_fail fail, $repeats restarts)" >&2
    exit 1
    ;;
  *)
    if [[ "$codex_fail" -gt 0 ]]; then
      exit 1
    fi
    exit 0
    ;;
esac
