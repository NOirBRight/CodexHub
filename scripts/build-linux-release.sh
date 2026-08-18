#!/usr/bin/env bash
# Build signed Linux AppImage + deb artifacts for one CodexHub flavor.
# Usage: scripts/build-linux-release.sh [--flavor normal|debug] [--skip-frontend] [--notes TEXT]
set -euo pipefail

flavor="normal"
skip_frontend=0
notes=""
release_base_url=""
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
private_key_path="${TAURI_SIGNING_PRIVATE_KEY:-$HOME/.codexhub/codexhub-updater.key}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --flavor)
      flavor="${2:-}"
      shift 2
      ;;
    --skip-frontend)
      skip_frontend=1
      shift
      ;;
    --notes)
      notes="${2:-}"
      shift 2
      ;;
    --private-key)
      private_key_path="${2:-}"
      shift 2
      ;;
    --release-base-url)
      release_base_url="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$flavor" != "normal" && "$flavor" != "debug" ]]; then
  echo "unknown build flavor: $flavor" >&2
  exit 2
fi

pwsh_bin="$(command -v pwsh || true)"
if [[ -z "$pwsh_bin" ]]; then
  echo "pwsh is required to generate the flavor Tauri config (install PowerShell 7)." >&2
  exit 1
fi

if [[ ! -f "$private_key_path" ]]; then
  echo "Updater private key was not found: $private_key_path" >&2
  exit 1
fi

generated_config="$("$pwsh_bin" -NoProfile -File "$repo_root/scripts/Build-TauriConfig.ps1" -Flavor "$flavor" -RepoRoot "$repo_root" | tr -d '\r')"
eval "$("$pwsh_bin" -NoProfile -File "$repo_root/scripts/_linux_release_meta.ps1" -Flavor "$flavor" -RepoRoot "$repo_root" -GeneratedConfig "$generated_config" | tr -d '\r')"

if [[ -z "${version:-}" ]]; then
  echo "failed to read generated Linux release metadata" >&2
  exit 1
fi

python3 - "$generated_config" <<'PY'
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text())
targets = cfg.get("bundle", {}).get("targets", [])
missing = [name for name in ("appimage", "deb") if name not in targets]
if missing:
    raise SystemExit(f"tauri.conf.json must include bundle.targets {missing} for Linux release")
if cfg.get("bundle", {}).get("createUpdaterArtifacts") is not True:
    raise SystemExit("tauri.conf.json must set bundle.createUpdaterArtifacts = true")
PY

if [[ "$skip_frontend" -eq 0 ]]; then
  export CODEXHUB_FRONTEND_PORT="$frontendPort"
  (
    cd "$repo_root/frontend"
    npm run build
  )
fi

export TAURI_SIGNING_PRIVATE_KEY="$private_key_path"
export CODEXHUB_BUILD_FLAVOR="$flavor"
export TAURI_CONFIG="$generated_config"
export CARGO_TARGET_DIR="$targetRoot"

tauri_args=(tauri build --config "$generated_config" --bundles appimage --bundles deb --ci)
if [[ "$flavor" == "debug" ]]; then
  tauri_args+=(--features debug-diagnostics)
fi
(
  cd "$repo_root/src-tauri"
  cargo "${tauri_args[@]}"
)

bundle_root="$targetRoot/release/bundle"
appimage_src="$(find "$bundle_root/appimage" -maxdepth 1 -name '*.AppImage' -type f | head -n 1 || true)"
deb_src="$(find "$bundle_root/deb" -maxdepth 1 -name '*.deb' -type f | head -n 1 || true)"
if [[ -z "$appimage_src" || -z "$deb_src" ]]; then
  echo "expected AppImage and deb were not generated under $bundle_root" >&2
  exit 1
fi

appimage_dst="$bundle_root/appimage/$appimageName"
deb_dst="$bundle_root/deb/$debName"
if [[ "$appimage_src" != "$appimage_dst" ]]; then
  mv -f "$appimage_src" "$appimage_dst"
  if [[ -f "$appimage_src.sig" ]]; then
    mv -f "$appimage_src.sig" "$appimage_dst.sig"
  fi
fi
if [[ "$deb_src" != "$deb_dst" ]]; then
  mv -f "$deb_src" "$deb_dst"
fi

if [[ ! -f "$appimage_dst.sig" ]]; then
  echo "expected updater signature was not generated: $appimage_dst.sig" >&2
  exit 1
fi

manifest_path="$bundle_root/$manifestName"
write_args=(
  -NoProfile
  -File "$repo_root/scripts/Write-LinuxReleaseManifest.ps1"
  -Flavor "$flavor"
  -Version "$version"
  -AppImagePath "$appimage_dst"
  -SignaturePath "$appimage_dst.sig"
  -ManifestPath "$manifest_path"
  -RepoRoot "$repo_root"
)
if [[ -n "$release_base_url" ]]; then
  write_args+=(-ReleaseBaseUrl "$release_base_url")
fi
if [[ -n "$notes" ]]; then
  write_args+=(-Notes "$notes")
fi
"$pwsh_bin" "${write_args[@]}"

echo "Linux release artifacts ready:"
echo "  AppImage: $appimage_dst"
echo "  Deb:      $deb_dst"
echo "  Signature:$appimage_dst.sig"
echo "  Manifest: $manifest_path"
echo "  Updater platform key: linux-x86_64"
if [[ -n "$notes" ]]; then
  echo "  Notes: $notes"
fi
