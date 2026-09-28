#!/usr/bin/env bash
set -euo pipefail

# This launcher's environment is scoped to one Claude Code process. Ordinary
# `claude` keeps its subscription route and all native subscription models.
settings_file="${CODEXHUB_GATEWAY_SETTINGS:-${CODEX_HOME:-$HOME/.codex}/proxy/settings.json}"

if ! command -v jq >/dev/null || ! command -v curl >/dev/null || ! command -v claude >/dev/null; then
  echo 'codexhub-claude-gateway requires jq, curl, and claude.' >&2
  exit 2
fi
if [[ ! -r "$settings_file" ]]; then
  echo 'CodexHub Gateway settings are unavailable.' >&2
  exit 2
fi

port="$(jq -er '.proxy_port | select(type == "number" and . >= 1 and . <= 65535)' "$settings_file")"
client_key="$(jq -er '.gateway_client_key | select(type == "string" and length > 0)' "$settings_file")"
base_url="http://127.0.0.1:$port"
models="$(curl --fail --silent --show-error --max-time 5 \
  -H 'anthropic-version: 2023-06-01' "$base_url/v1/models?limit=1000")"

if [[ "${1:-}" == --list ]]; then
  jq -r '.data[]?.id' <<<"$models"
  exit 0
fi
if [[ $# -lt 1 ]]; then
  echo 'Usage: codexhub-claude-gateway --list | MODEL [Claude Code arguments]' >&2
  exit 2
fi
gateway_model="$1"
shift
if ! jq -e --arg model "$gateway_model" '.data | any(.id == $model)' \
  <<<"$models" >/dev/null; then
  echo 'Model is not exported by the CodexHub Gateway; run --list to choose one.' >&2
  exit 2
fi
for argument in "$@"; do
  case "$argument" in
    --model|--model=*|--resume|--resume=*|--continue|-c|-r)
      echo 'Start a new Gateway session with this launcher; use ordinary claude for existing subscription sessions.' >&2
      exit 2
      ;;
  esac
done

unset ANTHROPIC_API_KEY CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
export ANTHROPIC_BASE_URL="$base_url"
export ANTHROPIC_AUTH_TOKEN="$client_key"
export ANTHROPIC_MODEL="$gateway_model"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
exec claude --model "$gateway_model" "$@"
