#!/usr/bin/env bash
# One-shot Linux local verification: Python core suite + Rust tests + clippy.
# Mirrors the Linux half of docs/agents/verification-policy.md.
# Windows cmd/PowerShell launcher and release-script tests skip on this host;
# they remain part of the Windows core suite.
#
# Each leg always runs. A failing leg is recorded and later legs still execute;
# the script exits non-zero if any leg failed.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_launcher="$repo_root/scripts/codexhub-python.sh"
if [[ ! -x "$python_launcher" ]]; then
  echo "error: missing $python_launcher" >&2
  exit 1
fi

failures=0

run_leg() {
  local name="$1"
  shift
  echo "==> $name"
  if "$@"; then
    echo "ok  $name"
  else
    local status=$?
    echo "FAIL $name (exit ${status})"
    failures=$((failures + 1))
  fi
}

run_leg "Python core (excluding real-client E2E)" \
  "$python_launcher" -m pytest -q --ignore=tests/test_real_client_e2e.py

run_leg "Python test partition completeness" \
  "$python_launcher" scripts/ci/check_python_test_partitions.py

run_leg "Rust tests" bash -c 'cd "$1/src-tauri" && cargo test --locked -- --test-threads=1' _ "$repo_root"

run_leg "Rust clippy" bash -c 'cd "$1/src-tauri" && cargo clippy --locked --all-targets -- -D warnings' _ "$repo_root"

echo "==> summary: ${failures} leg(s) failed"
if [[ "$failures" -eq 0 ]]; then
  echo "==> Linux local verification passed"
  exit 0
fi
exit 1
