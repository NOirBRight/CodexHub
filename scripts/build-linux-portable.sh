#!/usr/bin/env bash
# Build a release-optimized Linux portable tree for one CodexHub flavor.
# The patched runtime is prepared from the pinned upstream source and packaged by Tauri.
set -euo pipefail

flavor="normal"
output_root=""
dry_run=0
chatgpt_web_runtime=""
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --flavor)
      flavor="${2:-}"
      shift 2
      ;;
    --output-root)
      output_root="${2:-}"
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --chatgpt-web-runtime)
      chatgpt_web_runtime="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -n "$chatgpt_web_runtime" ]]; then
  if [[ ! -f "$chatgpt_web_runtime" ]]; then
    echo "ChatGPT Web runtime archive was not found: $chatgpt_web_runtime" >&2
    exit 2
  fi
  chatgpt_web_runtime="$(realpath "$chatgpt_web_runtime")"
fi

if [[ "$flavor" != "normal" && "$flavor" != "debug" ]]; then
  echo "unknown build flavor: $flavor" >&2
  exit 2
fi

if [[ -z "$output_root" ]]; then
  output_root="$repo_root/output/portable"
fi

pwsh_bin="$(command -v pwsh || true)"
if [[ -z "$pwsh_bin" ]]; then
  echo "pwsh is required to generate the flavor Tauri config (install PowerShell 7)." >&2
  exit 1
fi

generated_config="$("$pwsh_bin" -NoProfile -File "$repo_root/scripts/Build-TauriConfig.ps1" -Flavor "$flavor" -RepoRoot "$repo_root" | tr -d '\r')"
if [[ ! -f "$generated_config" ]]; then
  echo "failed to generate Tauri config for flavor $flavor" >&2
  exit 1
fi

meta_script="$repo_root/scripts/_linux_release_meta.ps1"
eval "$("$pwsh_bin" -NoProfile -File "$meta_script" -Flavor "$flavor" -RepoRoot "$repo_root" -GeneratedConfig "$generated_config" | tr -d '\r')"

if [[ -z "${version:-}" ]]; then
  echo "failed to read generated Linux release metadata" >&2
  exit 1
fi

source_revision="$(git -C "$repo_root" rev-parse HEAD)"
commit="${source_revision:0:8}"
portable_name="${releaseAssetPrefix}_${version}${releaseAssetSuffix}_linux_portable_${commit}"
portable_dir="$output_root/$portable_name"
portable_archive="$portable_dir.tar.gz"

if [[ "$dry_run" -eq 1 ]]; then
  cat <<EOF
{
  "flavor": "$flavor",
  "platform": "linux",
  "version": "$version",
  "source_revision": "$source_revision",
  "executable": "$executableBaseName",
  "portable_name": "$portable_name",
  "appimage_name": "$appimageName",
  "deb_name": "$debName",
  "updater_manifest": "$manifestName",
  "updater_platform": "linux-x86_64",
  "release_optimized": true,
  "debug_diagnostics_enabled": $([[ "$flavor" == "debug" ]] && echo true || echo false),
  "python_runtime": "host-python-3.13-plus-or-bundled-resources-python",
  "generated_config": {
    "productName": "$productName",
    "identifier": "$identifier",
    "title": "$windowTitle",
    "bridgePort": $bridgePort,
    "gatewayPort": $gatewayPort,
    "updaterEndpoint": "$updaterEndpoint"
  }
}
EOF
  exit 0
fi

runtime_args=(--repo-root "$repo_root")
if [[ -n "$chatgpt_web_runtime" ]]; then
  runtime_args+=(--archive "$chatgpt_web_runtime")
fi
"$repo_root/scripts/codexhub-python.sh" "$repo_root/scripts/prepare_chatgpt_web_runtime.py" "${runtime_args[@]}"

export CODEXHUB_FRONTEND_PORT="$frontendPort"
(
  cd "$repo_root/frontend"
  npm run build
)

mkdir -p "$output_root"
rm -rf "$portable_dir" "$portable_archive"
mkdir -p "$portable_dir"

export CODEXHUB_BUILD_FLAVOR="$flavor"
export CARGO_TARGET_DIR="$targetRoot"
tauri_args=(tauri build --config "$generated_config" --no-bundle --ci)
if [[ "$flavor" == "debug" ]]; then
  tauri_args+=(--features debug-diagnostics)
fi
(
  cd "$repo_root/src-tauri"
  cargo "${tauri_args[@]}"
)

binary="$targetRoot/release/codexhub"
if [[ ! -x "$binary" ]]; then
  echo "expected Linux binary was not produced: $binary" >&2
  exit 1
fi
cp -a "$binary" "$portable_dir/$executableBaseName"

# Copy declared source resources, never stale files from Cargo's output tree.
"$repo_root/scripts/codexhub-python.sh" - "$repo_root" "$portable_dir" "$generated_config" <<'PY'
import glob
import json
from pathlib import Path
import shutil
import sys

repo, portable, config = map(Path, sys.argv[1:])
resources = json.loads(config.read_text(encoding="utf-8"))["bundle"]["resources"]
for pattern, destination in resources.items():
    for match in glob.glob(str(repo / "src-tauri" / pattern), include_hidden=True):
        source = Path(match)
        target = portable / destination
        if glob.has_magic(pattern):
            target /= source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
PY
if [[ ! -f "$portable_dir/config/chatgpt_web_runtime_pin.json" ]] || \
   [[ ! -f "$portable_dir/config/codexhub-chatgpt-web-runtime-linux-x64.tar.gz" ]]; then
  echo "portable build is missing the patched ChatGPT Web Runtime resource" >&2
  exit 1
fi
if [[ ! -f "$portable_dir/scripts/xai_device_login.py" ]]; then
  echo "portable build is missing scripts/xai_device_login.py" >&2
  exit 1
fi

echo "==> GNOME dock icon E2E"
"$repo_root/scripts/codexhub-python.sh" \
  "$repo_root/scripts/e2e_linux_dock_icon.py" \
  --bin "$portable_dir/$executableBaseName"

if [[ ! -e "$portable_dir/python" ]]; then
  cat > "$portable_dir/LINUX_RUNTIME.txt" <<NOTE
This Linux portable build uses a host Python 3.13+ interpreter for the Gateway
unless you place a CPython at python/bin/python next to $executableBaseName.
Windows installers still ship an embedded runtime; Linux bundling of CPython is
optional and picked up automatically when present.
NOTE
fi

tar --exclude='__pycache__' --exclude='*.pyc' -C "$output_root" -czf "$portable_archive" "$portable_name"
sha256="$(sha256sum "$portable_archive" | awk '{print $1}')"
echo "Linux portable ready:"
echo "  Directory: $portable_dir"
echo "  Archive:   $portable_archive"
echo "  SHA256:    $sha256"
