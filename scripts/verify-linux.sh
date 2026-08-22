#!/usr/bin/env bash
# One-shot Linux local verification: Python core suite + Rust tests + clippy.
# Mirrors the Linux half of docs/agents/verification-policy.md.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_launcher="$repo_root/scripts/codexhub-python.sh"
if [[ ! -x "$python_launcher" ]]; then
  echo "error: missing $python_launcher" >&2
  exit 1
fi

echo "==> Python core (excluding real-client E2E)"
"$python_launcher" -m pytest -q --ignore=tests/test_real_client_e2e.py

echo "==> Python test partition completeness"
"$python_launcher" scripts/ci/check_python_test_partitions.py

echo "==> Rust tests"
(
  cd "$repo_root/src-tauri"
  cargo test --locked -- --test-threads=1
)

echo "==> Rust clippy"
(
  cd "$repo_root/src-tauri"
  cargo clippy --locked --all-targets -- -D warnings
)

echo "==> Linux local verification passed"
