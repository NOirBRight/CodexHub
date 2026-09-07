#!/usr/bin/env bash
# Build signed Linux AppImage + deb artifacts for one CodexHub flavor.
# Usage: scripts/build-linux-release.sh [--flavor normal|debug] [--skip-frontend] [--notes TEXT]
set -euo pipefail

flavor="normal"
skip_frontend=0
notes=""
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
private_key_path="${TAURI_SIGNING_PRIVATE_KEY:-$HOME/.codexhub/codexhub-updater.key}"
# linuxdeploy's bundled appimagetool occasionally fails to bootstrap this
# runtime even when the release asset itself is reachable. Keep the fallback
# byte-for-byte pinned so it fails closed if upstream replaces the asset.
appimage_runtime_url="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
appimage_runtime_sha256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

build_appimage_with_pinned_runtime() {
  local app_dir="$1"
  local appimage_path="$2"
  local cache_root="${XDG_CACHE_HOME:-$HOME/.cache}"
  local plugin_path="$cache_root/tauri/linuxdeploy-plugin-appimage.AppImage"
  local fallback_dir
  local runtime_path
  local appimagetool_path

  if [[ ! -d "$app_dir" ]]; then
    echo "AppImage fallback cannot find the prepared AppDir: $app_dir" >&2
    return 1
  fi
  if [[ ! -x "$plugin_path" ]]; then
    echo "AppImage fallback cannot find Tauri's appimage plugin: $plugin_path" >&2
    return 1
  fi
  if ! command -v curl >/dev/null; then
    echo "AppImage fallback requires curl to download the pinned runtime" >&2
    return 1
  fi

  fallback_dir="$(mktemp -d)"
  runtime_path="$fallback_dir/runtime-x86_64"
  curl --fail --location --retry 3 --retry-delay 2 --proto '=https' --tlsv1.2 \
    --output "$runtime_path" "$appimage_runtime_url"
  printf '%s  %s\n' "$appimage_runtime_sha256" "$runtime_path" | sha256sum --check --status

  (
    cd "$fallback_dir"
    "$plugin_path" --appimage-extract >/dev/null 2>&1
    appimagetool_path="$fallback_dir/squashfs-root/usr/bin/appimagetool"
    if [[ ! -x "$appimagetool_path" ]]; then
      echo "AppImage fallback could not extract appimagetool" >&2
      exit 1
    fi
    "$appimagetool_path" --runtime-file "$runtime_path" "$app_dir" "$appimage_path"
  )
  rm -rf "$fallback_dir"
}

has_prepared_appimage_dir() {
  local app_dir="$1"
  [[ -x "$app_dir/usr/bin/codexhub" ]] && \
    [[ -f "$app_dir/AppRun" ]] && \
    [[ -f "$app_dir/usr/share/applications/CodexHub.desktop" ]]
}

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

tauri_args=(tauri build --verbose --config "$generated_config" --bundles appimage --bundles deb --ci)
if [[ "$flavor" == "debug" ]]; then
  tauri_args+=(--features debug-diagnostics)
fi
bundle_log="$(mktemp)"
if ! (
  cd "$repo_root/src-tauri"
  cargo "${tauri_args[@]}"
) >"$bundle_log" 2>&1; then
  # Verbose Tauri output preserves the appimagetool marker. Only recover that
  # exact failure after linuxdeploy has completed the AppDir.
  if ! grep -Fq "Failed to download runtime file" "$bundle_log" || \
    ! has_prepared_appimage_dir "$bundle_root/CodexHub.AppDir"; then
    cat "$bundle_log" >&2
    rm -f "$bundle_log"
    exit 1
  fi

  echo "==> recovering AppImage after appimagetool runtime bootstrap failure"
  tauri_deb_args=(tauri build --config "$generated_config" --bundles deb --ci)
  if [[ "$flavor" == "debug" ]]; then
    tauri_deb_args+=(--features debug-diagnostics)
  fi
  (
    cd "$repo_root/src-tauri"
    cargo "${tauri_deb_args[@]}"
  )
  build_appimage_with_pinned_runtime \
    "$bundle_root/CodexHub.AppDir" \
    "$bundle_root/appimage/$appimageName"
  (
    cd "$repo_root/src-tauri"
    cargo tauri signer sign --private-key-path "$private_key_path" \
      "$bundle_root/appimage/$appimageName"
  )
fi
rm -f "$bundle_log"

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

deb_version="$(dpkg-deb -f "$deb_dst" Version)"
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
