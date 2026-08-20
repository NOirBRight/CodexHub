#!/usr/bin/env bash
# Source/development launcher for Linux. Prefer a host Python 3.13+ so
# scripts that invoke pytest through sys.executable keep development modules.
# Production/packaging entrypoints pass --prefer-bundled.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prefer_bundled=0
require_pytest=0
args=("$@")
if [[ ${#args[@]} -ge 1 && "${args[0]}" == "--prefer-bundled" ]]; then
  prefer_bundled=1
  args=("${args[@]:1}")
fi
if [[ ${#args[@]} -ge 2 && "${args[0]}" == "-m" && "${args[1]}" == "pytest" ]]; then
  require_pytest=1
fi

unset PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV CONDA_PREFIX \
  CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER PIPENV_ACTIVE CODEXHUB_RESOLVED_PYTHON

supports_python() {
  local path="$1"
  [[ -x "$path" ]] || return 1
  if ! "$path" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 13) else 1)' >/dev/null 2>&1; then
    return 1
  fi
  if [[ "$require_pytest" -eq 1 ]]; then
    "$path" -c 'import pytest' >/dev/null 2>&1 || return 1
  fi
  return 0
}

candidates=()
for env_name in CODEXHUB_E2E_PYTHON CODEXHUB_PYTHON CODEXHUB_PROXY_PYTHON; do
  value="${!env_name-}"
  if [[ -n "$value" ]]; then
    candidates+=("$value")
  fi
done

bundled=(
  "$repo_root/src-tauri/resources/python/bin/python"
  "$repo_root/src-tauri/resources/python/python"
)
local_venv=(
  "$repo_root/.venv/bin/python"
  "$repo_root/.venv-ci/bin/python"
)
host_names=(python3.14 python3.13 python3 python)

if [[ "$prefer_bundled" -eq 1 ]]; then
  search=("${bundled[@]}" "${local_venv[@]}" "${host_names[@]}")
else
  search=("${local_venv[@]}" "${host_names[@]}" "${bundled[@]}")
fi
candidates+=("${search[@]}")

resolved=""
for candidate in "${candidates[@]}"; do
  path="$candidate"
  if [[ "$path" != /* ]]; then
    if command -v "$path" >/dev/null 2>&1; then
      path="$(command -v "$path")"
    else
      continue
    fi
  fi
  if supports_python "$path"; then
    resolved="$path"
    break
  fi
done

if [[ -z "$resolved" ]]; then
  echo "CodexHub requires Python 3.13 or newer." >&2
  echo "Run scripts/codexhub-python.sh ..." >&2
  exit 127
fi

export CODEXHUB_PYTHON="$resolved"
export CODEXHUB_PROXY_PYTHON="$resolved"
export CODEXHUB_E2E_PYTHON="$resolved"
export PYTHONPATH="$repo_root/src-python"
export PATH="$(dirname "$resolved"):$repo_root/scripts:$PATH"

exec "$resolved" "${args[@]}"
