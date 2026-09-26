#!/usr/bin/env bash
# Build signed Linux AppImage + deb artifacts for one CodexHub flavor.
# Usage: scripts/build-linux-release.sh [--flavor normal|debug] [--skip-frontend] [--notes TEXT]
# Unsigned finalization: scripts/build-linux-release.sh --repack-only APPDIR OUTPUT
set -euo pipefail

flavor="normal"
skip_frontend=0
notes=""
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
private_key_path="${TAURI_SIGNING_PRIVATE_KEY:-$HOME/.codexhub/codexhub-updater.key}"
# Pin both packaging tools byte-for-byte; no privileged container is required.
appimage_runtime_url="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
appimage_runtime_sha256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

build_appimage_with_pinned_runtime() {
  local app_dir="$1"
  local appimage_path="$2"
  local fallback_dir runtime_path appimagetool_path
  if [[ ! -x "$app_dir/AppRun" || ! -x "$app_dir/CodexHub" ]]; then
    echo "AppImage requires a complete native AppDir: $app_dir" >&2
    return 1
  fi
  fallback_dir="$(mktemp -d)"
  runtime_path="$fallback_dir/runtime-x86_64"
  curl --fail --location --retry 3 --retry-delay 2 --proto '=https' --tlsv1.2 \
    --output "$runtime_path" "$appimage_runtime_url"
  printf '%s  %s\n' "$appimage_runtime_sha256" "$runtime_path" | sha256sum --check --status

  (
    cd "$fallback_dir"
    curl --fail --location --retry 3 --retry-delay 2 --proto '=https' --tlsv1.2 \
      --output appimagetool.AppImage \
      https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
    printf '%s  %s\n' ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0 appimagetool.AppImage | sha256sum --check --status
    chmod +x appimagetool.AppImage
    ./appimagetool.AppImage --appimage-extract >/dev/null
    appimagetool_path="$fallback_dir/squashfs-root/usr/bin/appimagetool"
    export PATH="$fallback_dir/squashfs-root/usr/bin:$PATH"
    rm -f "$appimage_path.sig"
    "$appimagetool_path" --runtime-file "$runtime_path" "$app_dir" "$appimage_path"
  )
  rm -rf "$fallback_dir"
}

if [[ "${1:-}" == "--repack-only" ]]; then
  [[ "$#" -eq 3 ]] || { echo "usage: $0 --repack-only APPDIR OUTPUT" >&2; exit 2; }
  build_appimage_with_pinned_runtime "$(realpath "$2")" "$(realpath -m "$3")"
  exit 0
fi

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

"$repo_root/scripts/codexhub-python.sh" - "$generated_config" <<'PY'
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

bundle_root="$targetRoot/release/bundle"
rm -rf "$bundle_root/appimage" "$bundle_root/deb"

# Build natively on Omarchy. Do not mix bundled GTK/Glycin with host drivers.
tauri_args=(tauri build --config "$generated_config" --bundles deb --ci)
if [[ "$flavor" == "debug" ]]; then
  tauri_args+=(--features debug-diagnostics)
fi
(
  cd "$repo_root/src-tauri"
  cargo "${tauri_args[@]}"
)
app_dir="$bundle_root/appimage/CodexHub.AppDir"
"$repo_root/scripts/prepare-linux-appdir.sh" \
  "$targetRoot/release" "$app_dir" "$repo_root/src-tauri/icons/128x128@2x.png"
build_appimage_with_pinned_runtime "$app_dir" "$bundle_root/appimage/$appimageName"

mapfile -t appimage_candidates < <(find "$bundle_root/appimage" -maxdepth 1 -name '*.AppImage' -type f -print)
mapfile -t deb_candidates < <(find "$bundle_root/deb" -maxdepth 1 -name '*.deb' -type f -print)
if [[ "${#appimage_candidates[@]}" -ne 1 ]]; then
  echo "expected exactly one AppImage under $bundle_root/appimage; found ${#appimage_candidates[@]}" >&2
  printf '  %s\n' "${appimage_candidates[@]}" >&2
  exit 1
fi
if [[ "${#deb_candidates[@]}" -ne 1 ]]; then
  echo "expected exactly one deb under $bundle_root/deb; found ${#deb_candidates[@]}" >&2
  printf '  %s\n' "${deb_candidates[@]}" >&2
  exit 1
fi
appimage_src="${appimage_candidates[0]}"
deb_src="${deb_candidates[0]}"

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

# Sign only the final shipped bytes.
(
  cd "$repo_root/src-tauri"
  unset TAURI_SIGNING_PRIVATE_KEY
  cargo tauri signer sign --private-key-path "$private_key_path" \
    --password "${TAURI_SIGNING_PRIVATE_KEY_PASSWORD:-}" "$appimage_dst"
)

deb_version="$(ar p "$deb_dst" control.tar.gz | tar -xzO control | sed -n 's/^Version: //p')"
if [[ "$deb_version" != "$version" ]]; then
  echo "deb package version mismatch: expected $version, found $deb_version in $deb_dst" >&2
  exit 1
fi

if [[ ! -f "$appimage_dst.sig" ]]; then
  echo "expected updater signature was not generated: $appimage_dst.sig" >&2
  exit 1
fi

manifest_path="$bundle_root/$manifestName"
signature="$(tr -d '\r\n' < "$appimage_dst.sig")"
if [[ -z "$signature" ]]; then
  echo "updater signature is empty: $appimage_dst.sig" >&2
  exit 1
fi
source_revision="$(git -C "$repo_root" rev-parse HEAD)"
if [[ -z "$source_revision" ]]; then
  echo "failed to resolve release source revision" >&2
  exit 1
fi
release_notes="${notes:-$productName $version}"
"$repo_root/scripts/codexhub-python.sh" - "$manifest_path" "$version" "$flavor" "$appimageName" "$signature" "$source_revision" "$release_notes" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
version, flavor, appimage_name, signature, source_revision, release_notes = sys.argv[2:8]
if not signature.strip():
    raise SystemExit("updater signature is empty")
base_url = f"https://github.com/NOirBRight/CodexHub/releases/download/v{version}"
existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
existing_flavor = existing.get("codexhub_flavor")
same_channel = existing.get("version") == version and (
    existing_flavor == flavor or (existing_flavor is None and flavor == "normal")
)
if not same_channel:
    existing = {}
platforms = existing.get("platforms", {})
if not isinstance(platforms, dict):
    platforms = {}
platforms = dict(platforms)
platforms["linux-x86_64"] = {
    "signature": signature.strip(),
    "url": f"{base_url}/{appimage_name}",
}
manifest = {
    **existing,
    "version": version,
    "codexhub_flavor": flavor,
    "codexhub_source_revision": source_revision,
    "notes": release_notes,
    "platforms": platforms,
}
manifest.setdefault("pub_date", datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY
echo "Linux release artifacts ready:"
echo "  AppImage: $appimage_dst"
echo "  Deb:      $deb_dst"
echo "  Signature:$appimage_dst.sig"
echo "  Manifest: $manifest_path"
echo "  Updater platform key: linux-x86_64"
if [[ -n "$notes" ]]; then
  echo "  Notes: $notes"
fi
